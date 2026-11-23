"""商务流水线：资料接收 → 触达解释 → 交付认定 → 补量 → 账款差额。

所有查询都从同一份 Ledger 读取；as_of 控制"截止某日已到达"的口径。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from .delivery import CREDITED, PENDING, REASON_LABELS, DeliveryEngine, DeliveryResult
from .identity import HouseholdGraph, merge_late_links
from .playback import PlaybackResult, build_play_units, raw_callback_totals
from .planning import Plan, PlanningEngine

# 商务语义：接受补量才产生承诺；上架回执决定素材实际可见起点。
ACCEPT_TOPIC = "makegood_acceptance"
ADJUST_TOPIC = "billing_adjustment"


@dataclass
class Snapshot:
    as_of: str
    tables: dict[str, list[dict]]
    playback: PlaybackResult
    graph: HouseholdGraph
    accepted_breaks: dict[str, set[str]]
    failed_breaks: dict[str, set[str]]
    online_breaks: dict[str, dict[str, str]]
    recognition_rows: list[dict]
    recognition_support: dict[str, dict]
    invoices: list[dict]
    delivery: dict[str, DeliveryResult]
    raw_totals: dict
    notes: list[str] = field(default_factory=list)

    def table(self, name: str) -> list[dict]:
        return self.tables[name]


def _accepted_breaks(
    tables: dict[str, list[dict]], decisions: Iterable[dict]
) -> tuple[dict[str, set[str]], list[str]]:
    """广告段占用：库存已承诺 + 已接受的补量申请 + 人工确认。

    客户只接受一部分时，只有可拆分的合同可以分别成立；整场独占不得拆分。
    任何没有接受记录的申请都不形成承诺。
    """
    accepted: dict[str, set[str]] = defaultdict(set)
    notes: list[str] = []
    for break_row in tables["ad_breaks"]:
        if break_row["accepted_contract"]:
            accepted[break_row["id"]].add(break_row["accepted_contract"])
    contracts = {r["id"]: r for r in tables["sponsorships"]}
    for request in tables["makegood_requests"]:
        contract = contracts[request["contract"]]
        accepted_list = list(request["accepted_breaks"])
        if not accepted_list:
            continue
        if not set(accepted_list) <= set(request["ad_breaks"]):
            notes.append(f"{request['id']} 接受了未申请的广告段，按部分接受处理")
            accepted_list = [b for b in accepted_list if b in request["ad_breaks"]]
        if request["all_or_none"] and set(accepted_list) != set(request["ad_breaks"]):
            notes.append(f"{request['id']} 要求整体落实但只接受部分，整场承诺不成立")
            accepted_list = []
        elif not contract["splittable"] and set(accepted_list) != set(request["ad_breaks"]):
            notes.append(f"{request['id']} 合同不可拆分，部分接受不形成承诺")
            accepted_list = []
        for break_id in accepted_list:
            accepted[break_id].add(request["contract"])
    for decision in decisions:
        if decision["topic"] != ACCEPT_TOPIC:
            continue
        payload = decision["payload"]
        for break_id in payload.get("ad_breaks", []):
            accepted[break_id].add(decision["subject"])
    return accepted, notes


def _failed_breaks(evidence: Iterable[dict], as_of: str) -> tuple[dict[str, set[str]], list[str]]:
    failed: dict[str, set[str]] = defaultdict(set)
    notes = []
    for item in evidence:
        if item["kind"] != "asset_failed":
            continue
        if item["received_on"] > as_of:
            continue
        contract = item["payload"].get("contract")
        if not contract:
            notes.append(f"{item['id']} 上架失败未指明合同，暂不归属任何合同")
            continue
        failed[item["subject"]].add(contract)
    return failed, notes


def _recognition_rows(
    tables: dict[str, list[dict]], as_of: str
) -> list[dict]:
    latest: dict[tuple[str, str], dict] = {}
    for row in tables["recognized_delivery"]:
        if row["recognized_on"] > as_of:
            continue
        key = (row["contract"], row["origin_event"])
        current = latest.get(key)
        if current is None or (
            row["recognition_version"],
            row["recognized_on"],
            row["id"],
        ) > (
            current["recognition_version"],
            current["recognized_on"],
            current["id"],
        ):
            latest[key] = row
    return sorted(latest.values(), key=lambda r: (r["recognized_on"], r["id"]))


def _apply_asset_receipts(
    tables: dict[str, list[dict]], evidence: Iterable[dict], as_of: str
) -> dict[str, dict[str, str]]:
    """平台上架回执：记录各合同素材在该广告段实际开始展示的日期。"""
    online: dict[str, dict[str, str]] = defaultdict(dict)
    for item in evidence:
        if item["received_on"] > as_of:
            continue
        if item["kind"] == "asset_online":
            payload = item["payload"]
            contract = payload["contract"]
            online[item["subject"]][contract] = payload.get("on", item["received_on"])
    return online


class Service:
    def __init__(self, ledger, as_of: str | None = None):
        self.ledger = ledger
        self.as_of = as_of
        self._snapshot: Snapshot | None = None
        self._extra_evidence: list[dict] = []

    def add_evidence(self, items: list[dict]) -> None:
        """接收调用方提供的迟到证据（也可先经 Ledger 落库）。"""
        self._extra_evidence.extend(dict(item) for item in items)
        self._snapshot = None

    def _late_ingested_evidence(self) -> list[dict]:
        return list(self._extra_evidence)

    def _cutoff(self, as_of: str | None) -> str:
        if as_of:
            return as_of
        if self.as_of:
            return self.as_of
        latest = ""
        for table in ("playback", "later_evidence", "recognized_delivery"):
            for row in self.ledger.table(table):
                for field_name in ("reported_on", "received_on", "recognized_on"):
                    value = row.get(field_name)
                    if value and value > latest:
                        latest = value
        return latest or "9999-12-31"

    def snapshot(self, refresh: bool = False) -> Snapshot:
        if self._snapshot is not None and not refresh:
            return self._snapshot
        as_of = self._cutoff(None)
        tables = self.ledger.all_tables()
        decisions = self.ledger.decisions()
        tables["later_evidence"] = list(tables["later_evidence"]) + self._late_ingested_evidence()
        evidence = [
            item
            for item in tables["later_evidence"]
            if item["received_on"] <= as_of
        ]
        playback = build_play_units(tables["playback"], evidence, as_of=as_of)
        links = merge_late_links(tables["household_links"], evidence)
        graph = HouseholdGraph(links)
        accepted, notes = _accepted_breaks(tables, decisions)
        failed, fail_notes = _failed_breaks(evidence, as_of)
        notes.extend(fail_notes)
        online = _apply_asset_receipts(tables, evidence, as_of)
        engine = DeliveryEngine(tables, graph, accepted, failed, online)
        delivery: dict[str, DeliveryResult] = {}
        for contract in tables["sponsorships"]:
            delivery[contract["id"]] = engine.evaluate(contract["id"], playback.units)
        recognition_rows = _recognition_rows(tables, as_of)
        support: dict[str, dict] = {}
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in recognition_rows:
            grouped[row["contract"]].append(row)
        for contract_id, rows in grouped.items():
            support[contract_id] = engine.reconcile_recognitions(
                contract_id, rows, playback.units
            )
        snapshot = Snapshot(
            as_of=as_of,
            tables=tables,
            playback=playback,
            graph=graph,
            accepted_breaks=accepted,
            failed_breaks=failed,
            online_breaks=online,
            recognition_rows=recognition_rows,
            recognition_support=support,
            invoices=tables["invoices"],
            delivery=delivery,
            raw_totals=raw_callback_totals(tables["playback"]),
            notes=notes,
        )
        self._snapshot = snapshot
        self._engine = engine
        return snapshot

    # ---------- 查询 ----------

    def contract_report(self, contract_id: str) -> dict:
        snap = self.snapshot()
        if contract_id not in snap.delivery:
            raise KeyError(f"未知合同: {contract_id}")
        contract = next(c for c in snap.tables["sponsorships"] if c["id"] == contract_id)
        result = snap.delivery[contract_id]
        support = snap.recognition_support.get(contract_id, {})
        invoices = [inv for inv in snap.invoices if inv["contract"] == contract_id]
        invoice_rows = [self.invoice_report(inv["id"]) for inv in invoices]
        unwanted = [d.to_dict() for d in result.decisions if d.status == PENDING]
        excluded = Counter()
        for decision in result.decisions:
            if decision.status == "excluded":
                excluded[decision.reason] += 1
        return {
            "contract": contract,
            "as_of": snap.as_of,
            "delivery": result.summary(),
            "raw": snap.raw_totals,
            "dedupe": {
                "mirror_rows_collapsed": snap.playback.mirrored_rows,
                "repeated_callback_units_collapsed": snap.playback.duplicate_callbacks,
                "units_after_dedupe": len(snap.playback.units),
                "conflicting_callback_units": snap.playback.conflict_units,
                "excluded_unit_count": snap.playback.excluded_units,
            },
            "excluded_units": dict(excluded),
            "excluded_unit_counts": {REASON_LABELS.get(k, k): v for k, v in excluded.items()},
            "pending_units": unwanted,
            "recognitions": support,
            "invoices": invoice_rows,
            "note": "触达/分钟的 upper 包含 possible 家庭，不能直接当作已交付。",
        }

    def contract_trace(self, contract_id: str) -> dict:
        snap = self.snapshot()
        result = snap.delivery[contract_id]
        credited = [
            d.to_dict()
            for d in result.decisions
            if d.status == CREDITED
        ]
        return {
            "contract": contract_id,
            "as_of": snap.as_of,
            "credited_units": credited,
            "counted_units": result.counted_units,
            "lower": result.lower,
            "upper": result.upper,
            "unlinked": result.unlinked,
            "frequency_drops": result.frequency_drops,
        }

    def ad_break_report(self, break_id: str) -> dict:
        snap = self.snapshot()
        if break_id not in {b["id"] for b in snap.tables["ad_breaks"]}:
            raise KeyError(f"未知广告段: {break_id}")
        break_row = next(b for b in snap.tables["ad_breaks"] if b["id"] == break_id)
        deliveries = []
        for contract_id, result in snap.delivery.items():
            units = [d for d in result.decisions if d.unit.ad_break == break_id]
            if units:
                deliveries.append(
                    {
                        "contract": contract_id,
                        "credited": sum(1 for d in units if d.status == CREDITED),
                        "pending": sum(1 for d in units if d.status == PENDING),
                        "excluded": sum(1 for d in units if d.status == "excluded"),
                        "units": [d.to_dict() for d in units][:50],
                    }
                )
        return {
            "ad_break": break_row,
            "accepted_contracts": sorted(snap.accepted_breaks.get(break_id, set())),
            "failed_contracts": sorted(snap.failed_breaks.get(break_id, set())),
            "deliveries": deliveries,
        }

    def invoice_report(self, invoice_id: str) -> dict:
        snap = self.snapshot()
        invoice = next((i for i in snap.tables["invoices"] if i["id"] == invoice_id), None)
        if invoice is None:
            raise KeyError(f"未知账单: {invoice_id}")
        contract = next(c for c in snap.tables["sponsorships"] if c["id"] == invoice["contract"])
        recognition_by_id = {r["id"]: r for r in snap.tables["recognized_delivery"]}
        support = snap.recognition_support.get(invoice["contract"], {})
        supported_ids = {
            item["recognition"]
            for item in support.get("items", [])
            if item.get("supported")
        }
        invoice_supported_units = 0.0
        latest = {(r["contract"], r["origin_event"]): r for r in snap.recognition_rows}
        for rec_id in invoice["recognitions"]:
            row = recognition_by_id.get(rec_id)
            if row is None:
                continue
            effective = latest.get((row["contract"], row["origin_event"]), row)
            item = next(
                (i for i in support.get("items", []) if i["recognition"] == effective["id"]),
                None,
            )
            if item and item.get("supported"):
                invoice_supported_units += effective["credited_units"]
        supported_amount = round(invoice_supported_units * contract["unit_price_minor"])
        return {
            "invoice": invoice,
            "contract": contract["id"],
            "supported_recognitions": sorted(supported_ids),
            "supported_units": round(invoice_supported_units, 3),
            "supported_amount_minor": supported_amount,
            "variance_minor": invoice["amount_minor"] - supported_amount,
            "paid_minor": invoice["paid_minor"],
            "outstanding_minor": invoice["amount_minor"] - invoice["paid_minor"],
            "note": "差额不推翻已开票与已收款事实，仅说明当前证据可支持的范围。",
        }

    # ---------- 计划 ----------

    def plans(self, level: str = "balanced", contract_id: str | None = None) -> list[Plan]:
        snap = self.snapshot()
        engine = PlanningEngine(
            snap.tables,
            snap.graph,
            self._engine,
            snap.delivery,
            snap.accepted_breaks,
            snap.as_of,
            self.ledger.decisions(),
            snap.failed_breaks,
            snap.online_breaks,
        )
        if contract_id:
            return [engine.plan(contract_id, level)]
        return engine.plan_portfolio(level)

    # ---------- 人工决定 ----------

    def accept_makegood(self, request_id: str, break_ids: list[str], on: str) -> dict:
        snap = self.snapshot()
        request = next((r for r in snap.tables["makegood_requests"] if r["id"] == request_id), None)
        if request is None:
            raise KeyError(f"未知补量申请: {request_id}")
        contract = next(c for c in snap.tables["sponsorships"] if c["id"] == request["contract"])
        if not set(break_ids) <= set(request["ad_breaks"]):
            raise ValueError("接受的广告段必须来自原申请")
        if request["all_or_none"] and set(break_ids) != set(request["ad_breaks"]):
            raise ValueError("该申请要求整体落实，不能拆分接受")
        if not contract["splittable"] and set(break_ids) != set(request["ad_breaks"]):
            raise ValueError("该合同不允许拆分接受")
        decision_id = self.ledger.decide(
            ACCEPT_TOPIC,
            request["contract"],
            {"request": request_id, "ad_breaks": sorted(set(break_ids))},
            on,
        )
        self._snapshot = None
        return {"decision": decision_id, "contract": request["contract"], "ad_breaks": sorted(set(break_ids))}

    def adjust_billing(self, contract_id: str, on: str, reason: str, amount_minor: int) -> dict:
        decision_id = self.ledger.decide(
            ADJUST_TOPIC,
            contract_id,
            {"reason": reason, "amount_minor": amount_minor},
            on,
        )
        self._snapshot = None
        return {"decision": decision_id, "contract": contract_id, "amount_minor": amount_minor}

    # ---------- 解释 ----------

    def reach_explanation(self, contract_id: str) -> dict:
        """触达/分钟的范围与口径说明，供商务直接阅读。"""
        result = self.snapshot().delivery[contract_id]
        contract = result.contract
        unresolved = [
            d for d in result.decisions if d.entity and d.entity[0] == "possible"
        ]
        unlinked = [d for d in result.decisions if d.entity and d.entity[0] == "device"]
        return {
            "contract": contract_id,
            "metric": contract["metric"],
            "definitions": {
                "raw": "收到的全部平台回传，含镜像与重复",
                "qualified": "通过合同条件但尚未施加频次上限",
                "recognized": "商务已签发、当前证据可支持的认定",
            },
            "lower": result.lower,
            "upper": result.upper,
            "unlinked_units": result.unlinked,
            "possible_household_units": len(unresolved),
            "unlinked_device_units": len(unlinked),
            "frequency_cap": contract["frequency_cap"],
            "frequency_drops": result.frequency_drops,
            "raw_rows": self.snapshot().raw_totals["raw_rows"],
            "origin_events": self.snapshot().raw_totals["origin_events"],
            "play_units": len(self.snapshot().playback.units),
            "pending_unit_reasons": dict(
                Counter(d.reason for d in result.decisions if d.status == PENDING)
            ),
            "notes": list(self.snapshot().notes),
            "caveats": [
                "upper 将 possible 家庭按可能同一户计入，不应当作确定触达。",
                "没有联系证据的设备单列，不并入任何家庭。",
                "迟到家庭合并证据只在其 effective_from 后生效，received_on 前的认定不追溯。",
            ],
        }
