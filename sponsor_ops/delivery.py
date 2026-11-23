"""按合同认定合格交付。

分为三层：
1. 原始播放：收到的全部回传（含镜像、重复）。
2. 合格交付：通过合同地区、受众层、平台、素材、可见性条件，并通过广告段
   排他与竞品规则的播放单元。
3. 已认定交付：商务已经签发、且能被当前证据支持的认定（recognized_delivery）。

几何频次：
- households 指标：每户合同期只计一次，频次上限只影响曝光次数，不影响触达家庭。
- view_minutes 指标：每户最多计入 frequency_cap 个合格曝光，超出的不计入。
触达/分钟都给出确定范围：
- lower：只计明确联系家庭。
- upper：明确联系 + 尚未裁定的可能联系（按"可能同一户"的上界）。
- unlinked：没有任何联系证据，单列，不并入住户数。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .identity import Entity, HouseholdGraph
from .playback import PlayUnit

# 认定状态
CREDITED = "credited"
EXCLUDED = "excluded"
PENDING = "pending"

REASON_LABELS = {
    "consent": "设备未同意用于赞助计量",
    "region": "设备地区不在合同范围",
    "segment": "受众层不在合同范围",
    "platform": "转播平台不在合同有效渠道",
    "period": "场次不在合同期",
    "asset": "素材版本未获客户认可",
    "background": "后台播放",
    "uncovered": "缺可见性监测",
    "short": "可见不足30秒",
    "asset_failed": "广告段上架失败",
    "break_occupied": "排他广告段已归属其他合同",
    "competitor_conflict": "同品类竞品争用同一排他广告段",
    "exclusive_conflict": "排他广告段存在未决归属",
    "parallel_not_allowed": "非排他段并计未获合同允许",
    "frequency_cap": "该户已用满合同期频次上限",
    "household_duplicate": "同一家庭已被前一条认定覆盖",
    "online_after_match": "素材上架回执晚于该场次，不能算已交付",
    "unknown_event": "认定引用的播放事件不存在",
}


@dataclass
class UnitDecision:
    unit: PlayUnit
    status: str
    reason: str | None
    entity: Entity | None = None
    counted_lower: float = 0.0
    counted_upper: float = 0.0
    counted_household: float = 0.0
    note: str | None = None

    def to_dict(self) -> dict:
        return {
            "origin_events": list(self.unit.origin_events),
            "device": self.unit.device,
            "match": self.unit.match,
            "ad_break": self.unit.ad_break,
            "asset_version": self.unit.asset_version,
            "visible_lower": round(self.unit.visible_lower, 3),
            "visible_upper": round(self.unit.visible_upper, 3),
            "callbacks": self.unit.callbacks,
            "reported_on": self.unit.reported_on,
            "status": self.status,
            "reason": self.reason,
            "reason_label": REASON_LABELS.get(self.reason, self.reason),
            "entity": list(self.entity) if self.entity else None,
            "counted_lower": round(self.counted_lower, 3),
            "counted_upper": round(self.counted_upper, 3),
            "counted_household": self.counted_household,
            "note": self.note,
        }


@dataclass
class DeliveryResult:
    contract: dict
    decisions: list[UnitDecision]
    lower: float
    upper: float
    unlinked: float
    counted_units: float
    gap_lower: float
    gap_upper: float
    by_reason: dict[str, int]
    frequency_drops: int
    pending_units: int
    extra: dict = field(default_factory=dict)

    def summary(self) -> dict:
        c = self.contract
        return {
            "contract": c["id"],
            "metric": c["metric"],
            "target": c["target"],
            "unit_price_minor": c["unit_price_minor"],
            "lower": round(self.lower, 3),
            "upper": round(self.upper, 3),
            "unlinked": round(self.unlinked, 3),
            "gap_lower": round(self.gap_lower, 3),
            "gap_upper": round(self.gap_upper, 3),
            "frequency_drops": self.frequency_drops,
            "by_reason": dict(sorted(self.by_reason.items())),
            "pending_units": self.pending_units,
            **self.extra,
        }


def _basic_reason(contract: dict, unit: PlayUnit, devices: dict, matches: dict) -> str | None:
    device = devices[unit.device]
    match = matches[unit.match]
    if not device["consent"]:
        return "consent"
    if device["region"] not in contract["regions"]:
        return "region"
    if device["segment"] not in contract["segments"]:
        return "segment"
    if match["platform"] not in contract["platforms"]:
        return "platform"
    if not (contract["period_from"] <= match["on"] <= contract["period_to"]):
        return "period"
    if unit.asset_version not in contract["asset_versions"]:
        return "asset"
    issue = unit.quality_issue()
    if issue:
        return issue
    return None


class DeliveryEngine:
    def __init__(
        self,
        tables: dict[str, list[dict]],
        graph: HouseholdGraph,
        accepted_breaks: dict[str, set[str]] | None = None,
        failed_breaks: dict[str, set[str]] | None = None,
        online_breaks: dict[str, dict[str, str]] | None = None,
    ):
        self.devices = {r["id"]: r for r in tables["devices"]}
        self.matches = {r["id"]: r for r in tables["matches"]}
        self.breaks = {r["id"]: r for r in tables["ad_breaks"]}
        self.contracts = {r["id"]: r for r in tables["sponsorships"]}
        self.graph = graph
        self.accepted_breaks = accepted_breaks or {}
        self.failed_breaks = failed_breaks or {}
        self.online_breaks = online_breaks or {}

    # ----- 广告段归属 -----

    def break_claimants(self, units: list[PlayUnit]) -> dict[str, dict[str, list[PlayUnit]]]:
        """每个广告段中，各合同仅按基础条件即可主张的播放。"""
        claims: dict[str, dict[str, list[PlayUnit]]] = defaultdict(lambda: defaultdict(list))
        for contract in self.contracts.values():
            for unit in units:
                if unit.asset_version not in contract["asset_versions"]:
                    continue
                if _basic_reason(contract, unit, self.devices, self.matches) is None:
                    claims[unit.ad_break][contract["id"]].append(unit)
        return claims

    def unit_status(
        self, contract: dict, unit: PlayUnit, claims: dict[str, dict[str, list[PlayUnit]]]
    ) -> tuple[str, str | None]:
        reason = _basic_reason(contract, unit, self.devices, self.matches)
        if reason:
            return EXCLUDED, reason
        break_row = self.breaks[unit.ad_break]
        if contract["id"] in self.failed_breaks.get(unit.ad_break, set()):
            return EXCLUDED, "asset_failed"
        online_on = self.online_breaks.get(unit.ad_break, {}).get(contract["id"])
        if online_on and self.matches[unit.match]["on"] < online_on:
            return EXCLUDED, "online_after_match"
        occupants = self.accepted_breaks.get(unit.ad_break, set())
        if break_row["exclusive"]:
            if occupants:
                if contract["id"] not in occupants:
                    return EXCLUDED, "break_occupied"
            else:
                claimants = claims.get(unit.ad_break, {})
                if len(claimants) > 1:
                    categories = {self.contracts[cid]["category"] for cid in claimants}
                    return PENDING, (
                        "competitor_conflict" if len(categories) == 1 else "exclusive_conflict"
                    )
                if set(claimants) != {contract["id"]}:
                    return PENDING, "exclusive_conflict"
        else:
            claimants = claims.get(unit.ad_break, {})
            if len(claimants) > 1:
                if not all(self.contracts[cid]["allow_parallel_credit"] for cid in claimants):
                    return PENDING, "parallel_not_allowed"
        return CREDITED, None

    # ----- 认定 -----

    def evaluate(self, contract_id: str, units: list[PlayUnit]) -> DeliveryResult:
        contract = self.contracts[contract_id]
        claims = self.break_claimants(units)
        decisions: list[UnitDecision] = []
        for unit in units:
            if unit.asset_version not in contract["asset_versions"]:
                continue
            status, reason = self.unit_status(contract, unit, claims)
            decisions.append(
                UnitDecision(
                    unit=unit,
                    status=status,
                    reason=reason,
                    entity=self.graph.resolve(unit.device, self.matches[unit.match]["on"]),
                )
            )
        return self._settle(contract, decisions)

    def _settle(self, contract: dict, decisions: list[UnitDecision]) -> DeliveryResult:
        metric = contract["metric"]
        cap = contract["frequency_cap"]
        credited = [d for d in decisions if d.status == CREDITED]
        # 按场次时间排序并施加频次上限
        credited.sort(
            key=lambda d: (
                self.matches[d.unit.match]["on"],
                self.breaks[d.unit.ad_break]["start_seconds"],
                d.unit.origin_events[0] if d.unit.origin_events else "",
            )
        )
        per_entity: dict[Entity, int] = defaultdict(int)
        drops = 0
        kept: list[UnitDecision] = []
        for decision in credited:
            entity = decision.entity
            if metric == "households":
                # 家庭触达每户只计一次；频次上限不减少触达家庭
                per_entity[entity] += 1
                kept.append(decision)
                continue
            if per_entity[entity] >= cap:
                drops += 1
                decision.status = EXCLUDED
                decision.reason = "frequency_cap"
                decision.note = "该户已用满合同期频次上限"
                continue
            per_entity[entity] += 1
            decision.counted_lower = decision.unit.visible_lower / 60.0
            decision.counted_upper = decision.unit.visible_upper / 60.0
            kept.append(decision)
        lower = upper = unlinked = 0.0
        if metric == "households":
            distinct: dict[Entity, None] = {}
            for decision in kept:
                distinct.setdefault(decision.entity, None)
                decision.counted_household = 1.0 if decision.entity[0] != "device" else 0.0
            lower = float(sum(1 for e in distinct if e[0] == "household"))
            upper = float(sum(1 for e in distinct if e[0] in ("household", "possible")))
            unlinked = float(sum(1 for e in distinct if e[0] == "device"))
            counted = float(len(distinct))
        else:
            for decision in kept:
                kind = decision.entity[0]
                if kind == "household":
                    lower += decision.counted_lower
                    upper += decision.counted_upper
                elif kind == "possible":
                    upper += decision.counted_upper
                else:
                    unlinked += decision.counted_lower
            counted = float(len(kept))
        reasons = Counter(d.reason for d in decisions if d.status == EXCLUDED and d.reason)
        pending = sum(1 for d in decisions if d.status == PENDING)
        gap_lower = max(0.0, contract["target"] - lower)
        gap_upper = max(0.0, contract["target"] - upper)
        extra = {
            "qualified_units": len(credited),
            "counted_units": len(kept),
            "excluded_units": len(decisions) - len(credited) - pending,
        }
        return DeliveryResult(
            contract=contract,
            decisions=decisions,
            lower=lower,
            upper=upper,
            unlinked=unlinked,
            counted_units=counted,
            gap_lower=gap_lower,
            gap_upper=gap_upper,
            by_reason=dict(reasons),
            frequency_drops=drops,
            pending_units=pending,
            extra=extra,
        )

    # ----- 与商务认定对账 -----

    def reconcile_recognitions(
        self,
        contract_id: str,
        recognitions: list[dict],
        units: list[PlayUnit],
    ) -> dict:
        """校验商务认定：返回可支持的认定与由此形成的差额。"""
        contract = self.contracts[contract_id]
        by_origin = {origin: unit for unit in units for origin in unit.origin_events}
        claims = self.break_claimants(units)
        result = self.evaluate(contract_id, units)
        supported_ids = {
            d.unit.origin_events[0]
            for d in result.decisions
            if d.status in (CREDITED,)
        }
        items = []
        supported_units = 0.0
        claimed_units = 0.0
        seen_entities: dict[Entity, int] = defaultdict(int)
        for rec in sorted(recognitions, key=lambda r: (r["recognized_on"], r["id"])):
            claimed_units += rec["credited_units"]
            unit = by_origin.get(rec["origin_event"])
            if unit is None:
                items.append(
                    {
                        "recognition": rec["id"],
                        "supported": False,
                        "reason": "unknown_event",
                        "supported_value": 0.0,
                        "recognized_value": rec["credited_units"],
                    }
                )
                continue
            status, reason = self.unit_status(contract, unit, claims)
            if status != CREDITED:
                items.append(
                    {
                        "recognition": rec["id"],
                        "supported": False,
                        "reason": reason or status,
                        "reason_label": REASON_LABELS.get(reason, reason),
                        "supported_value": 0.0,
                        "recognized_value": rec["credited_units"],
                    }
                )
                continue
            entity = self.graph.resolve(unit.device, self.matches[unit.match]["on"])
            value = 1.0
            if contract["metric"] == "households":
                if seen_entities[entity] >= 1:
                    items.append(
                        {
                            "recognition": rec["id"],
                            "supported": False,
                            "reason": "household_duplicate",
                            "supported_value": 0.0,
                            "recognized_value": rec["credited_units"],
                        }
                    )
                    continue
                seen_entities[entity] += 1
                supported_units += 1.0
            else:
                if seen_entities[entity] >= contract["frequency_cap"]:
                    items.append(
                        {
                            "recognition": rec["id"],
                            "supported": False,
                            "reason": "frequency_cap",
                            "supported_value": 0.0,
                            "recognized_value": rec["credited_units"],
                        }
                    )
                    continue
                seen_entities[entity] += 1
                value = unit.visible_lower / 60.0
                supported_units += value
            items.append(
                {
                    "recognition": rec["id"],
                    "supported": True,
                    "reason": None,
                    "supported_value": round(value, 3),
                    "recognized_value": rec["credited_units"],
                }
            )
        supported_amount = round(supported_units * contract["unit_price_minor"])
        claimed_amount = round(claimed_units * contract["unit_price_minor"])
        return {
            "contract": contract_id,
            "claimed_units": round(claimed_units, 3),
            "supported_units": round(supported_units, 3),
            "unsupported_units": round(claimed_units - supported_units, 3),
            "claimed_amount_minor": claimed_amount,
            "supported_amount_minor": supported_amount,
            "variance_minor": claimed_amount - supported_amount,
            "items": items,
            "recognized_event_ids": sorted(supported_ids),
        }
