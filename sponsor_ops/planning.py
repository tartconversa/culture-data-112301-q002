"""补量计划：未来场次、受众调查、广告段库存与预算。

关键口径：
- 同一调查家庭跨场次只算一次；已知家庭与已认定触达先生成去重集合。
- 同一场次已经安排广告段后，再加同场次广告段不新增触达；对分钟指标只按
  频次上限增加重复曝光，不把同场观众当成新家庭。
- 频次上限按合同期累计，已到上限的家庭不再产生新增分钟收益。
- 计划逐段增量计算，因此预算、缺口和竞品争用会实时变化，不能把每场预测
  家庭数简单相加。
- 事后调查只用于回测校准；没有同地区、同渠道可比历史的场次保留缺证范围。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from .delivery import CREDITED, DeliveryEngine, DeliveryResult
from .identity import Entity, HouseholdGraph

LEVELS = ("conservative", "balanced", "aggressive")

LEVEL_LABELS = {
    "conservative": "保守",
    "balanced": "平衡",
    "aggressive": "进取",
}


@dataclass
class BreakForecast:
    break_id: str
    match: str
    on: str
    platform: str
    region: str
    exclusive: bool
    cost_minor: int
    available: bool
    blocked_reason: str | None
    known_new: float = 0.0
    known_weight: float = 0.0
    unknown_new: float = 0.0
    unknown_weight: float = 0.0
    repeat_known: float = 0.0
    avg_minutes: float = 0.0
    calibration_ratio: float = 0.0
    panel_rows: int = 0
    linked_rows: int = 0
    already_reached: int = 0
    capped_households: int = 0
    evidence_gap: str | None = None
    surveys_usable: bool = False
    notes: list[str] = field(default_factory=list)

    def population(self) -> float:
        """按纳入概率还原的去重受众规模上界。"""
        return self.known_weight + self.unknown_weight

    def reach_value(self, level: str) -> float:
        if level == "conservative":
            return self.known_new
        if level == "aggressive":
            return self.population()
        # 平衡口径按调查去重后的确知新增与未知新增分别折算，不把未知样本
        # 当作等同确定家庭。
        return self.known_new + self.calibration_ratio * (
            self.known_weight - self.known_new + self.unknown_new
        )

    def value(self, metric: str, level: str) -> float:
        households = self.reach_value(level)
        if metric == "households":
            return households
        return (households + self.repeat_known) * self.avg_minutes

    def reach_range(self) -> tuple[float, float]:
        return self.known_new, self.population()

    def to_dict(self) -> dict:
        return {
            "break": self.break_id,
            "match": self.match,
            "on": self.on,
            "platform": self.platform,
            "region": self.region,
            "exclusive": self.exclusive,
            "cost_minor": self.cost_minor,
            "available": self.available,
            "blocked_reason": self.blocked_reason,
            "known_new_households": round(self.known_new, 3),
            "known_weighted_households": round(self.known_weight, 3),
            "unknown_weighted_households": round(self.unknown_weight, 3),
            "unknown_new_sample_households": round(self.unknown_new, 3),
            "repeat_known_households": round(self.repeat_known, 3),
            "households_range": [round(self.known_new, 3), round(self.population(), 3)],
            "avg_minutes": round(self.avg_minutes, 3),
            "calibration_ratio": round(self.calibration_ratio, 4),
            "panel_rows": self.panel_rows,
            "linked_rows": self.linked_rows,
            "already_reached": self.already_reached,
            "capped_households": self.capped_households,
            "evidence_gap": self.evidence_gap,
            "surveys_usable": self.surveys_usable,
            "notes": list(self.notes),
        }


@dataclass
class Plan:
    contract: dict
    level: str
    as_of: str
    gap_lower: float
    gap_upper: float
    selected: list[dict]
    skipped: list[dict]
    spend_minor: int
    budget_minor: int
    covered_lower: float
    covered_upper: float
    remaining_lower: float
    remaining_upper: float
    unguaranteed: float
    warnings: list[str] = field(default_factory=list)
    calibration: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "contract": self.contract["id"],
            "metric": self.contract["metric"],
            "level": self.level,
            "level_label": LEVEL_LABELS[self.level],
            "as_of": self.as_of,
            "target": self.contract["target"],
            "gap_lower": round(self.gap_lower, 3),
            "gap_upper": round(self.gap_upper, 3),
            "selected": self.selected,
            "skipped": self.skipped,
            "spend_minor": self.spend_minor,
            "budget_minor": self.budget_minor,
            "covered_lower": round(self.covered_lower, 3),
            "covered_upper": round(self.covered_upper, 3),
            "remaining_lower": round(self.remaining_lower, 3),
            "remaining_upper": round(self.remaining_upper, 3),
            "unguaranteed": round(self.unguaranteed, 3),
            "warnings": list(self.warnings),
            "calibration": self.calibration,
        }


class PlanningEngine:
    def __init__(
        self,
        tables: dict[str, list[dict]],
        graph: HouseholdGraph,
        delivery: DeliveryEngine,
        results: dict[str, DeliveryResult],
        accepted_breaks: dict[str, set[str]],
        as_of: str,
        decisions: Iterable[dict] = (),
        failed_breaks: dict[str, set[str]] | None = None,
        online_breaks: dict[str, dict[str, str]] | None = None,
    ):
        self.devices = {r["id"]: r for r in tables["devices"]}
        self.matches = {r["id"]: r for r in tables["matches"]}
        self.breaks = {r["id"]: r for r in tables["ad_breaks"]}
        self.contracts = {r["id"]: r for r in tables["sponsorships"]}
        self.panels = tables["audience_panels"]
        self.graph = graph
        self.delivery = delivery
        self.results = results
        self.accepted_breaks = accepted_breaks
        self.failed_breaks = failed_breaks or {}
        self.online_breaks = online_breaks or {}
        self.as_of = as_of
        self.decisions = list(decisions)

    # ---------- 历史校准 ----------

    def calibration(self, contract_id: str) -> dict:
        """事后校准：调查到达后与计量家庭对比，只用于估计覆盖比例。

        回测严格使用场次当日或之后到达的调查；签约前不可能知道的事后调查
        只说明预测偏差，不进入未来预测。
        """
        contract = self.contracts[contract_id]
        result = self.results[contract_id]
        completed = {
            m["id"]: m
            for m in self.matches.values()
            if m["state"] == "completed" and m["on"] <= self.as_of
        }
        weighted = 0.0
        seen = set()
        surveyed_matches = set()
        for row in self.panels:
            match = completed.get(row["match"])
            if match is None or row["surveyed_on"] > self.as_of:
                continue
            surveyed_matches.add(row["match"])
            if not row["attended"]:
                continue
            if row["region"] not in contract["regions"] or row["segment"] not in contract["segments"]:
                continue
            if match["region"] not in contract["regions"] or match["platform"] not in contract["platforms"]:
                continue
            if row["panel_household"] in seen:
                continue
            seen.add(row["panel_household"])
            weighted += 1.0 / row["inclusion_probability"]
        metered = result.lower
        ratio = (metered / weighted) if weighted else None
        return {
            "method": "场次结束后到达的调查只做事后校准，不当作事前实绩",
            "completed_matches_with_survey": len(surveyed_matches),
            "panel_attended_weighted": round(weighted, 3),
            "metered_lower": round(metered, 3),
            "coverage_ratio": round(ratio, 4) if ratio is not None else None,
            "note": "覆盖率低于 1 表示调查意愿高于计量触达，平衡估计按该比例折算未知部分。",
        }

    # ---------- 预测 ----------

    def _survey_rows(self, contract: dict, match: dict) -> list[dict]:
        rows = []
        for row in self.panels:
            if row["match"] != match["id"]:
                continue
            if row["surveyed_on"] > self.as_of:
                continue
            if row["region"] not in contract["regions"]:
                continue
            if row["segment"] not in contract["segments"]:
                continue
            rows.append(row)
        return rows

    def _historical_pairs(self, contract: dict) -> set[tuple[str, str]]:
        pairs = set()
        for match in self.matches.values():
            if match["state"] != "completed" or match["on"] > self.as_of:
                continue
            if match["region"] in contract["regions"] and match["platform"] in contract["platforms"]:
                pairs.add((match["region"], match["platform"]))
        return pairs

    def _reached_households(self, contract_id: str) -> set[str]:
        reached = set()
        for decision in self.results[contract_id].decisions:
            if decision.status != CREDITED:
                continue
            entity = decision.entity
            if entity and entity[0] == "household":
                reached.add(entity[1])
        return reached

    def _entity_exposure(self, contract_id: str) -> dict[Entity, int]:
        counts: dict[Entity, int] = defaultdict(int)
        for decision in self.results[contract_id].decisions:
            if decision.status == CREDITED and decision.entity is not None:
                counts[decision.entity] += 1
        return counts

    def _average_minutes(self, contract_id: str) -> float:
        result = self.results[contract_id]
        units = [d for d in result.decisions if d.status == CREDITED]
        if not units:
            return 0.0
        return sum(d.unit.visible_lower for d in units) / len(units) / 60.0

    def _blocked(
        self, contract: dict, break_row: dict, match: dict
    ) -> str | None:
        if match["state"] != "scheduled" or match["on"] < self.as_of:
            return "not_future_match"
        if not (contract["period_from"] <= match["on"] <= contract["period_to"]):
            return "outside_contract_period"
        if match["region"] not in contract["regions"]:
            return "region_not_in_contract"
        if match["platform"] not in contract["platforms"]:
            return "platform_not_in_contract"
        occupants = self.accepted_breaks.get(break_row["id"], set())
        if occupants:
            if contract["id"] in occupants:
                return "already_committed"
            return "break_committed_to_other_contract"
        if break_row["exclusive"] and break_row["accepted_contract"] not in (None, contract["id"]):
            return "exclusive_break_owned_by_other_contract"
        if contract["id"] in self.failed_breaks.get(break_row["id"], set()):
            return "asset_failed"
        online = self.online_breaks.get(break_row["id"], {})
        if online:
            if contract["id"] not in online:
                return "break_online_for_other_contract"
            if online[contract["id"]] > match["on"]:
                return "online_after_match"
        return None

    def _baseline_exposure(self, contract_id: str) -> Counter:
        counts: Counter = Counter()
        for decision in self.results[contract_id].decisions:
            if decision.status != CREDITED or decision.entity is None:
                continue
            kind, name = decision.entity
            if kind == "household":
                counts[("h", name)] += 1
        return counts

    def _fresh_state(self, contract_id: str) -> dict:
        return {
            "exposure": self._baseline_exposure(contract_id),
            "reached": self._reached_households(contract_id),
            "reach_keys": set(),
            "selected_matches": set(),
        }

    def forecast_break(
        self,
        contract: dict,
        break_row: dict,
        state: dict,
    ) -> BreakForecast:
        match = self.matches[break_row["match"]]
        forecast = BreakForecast(
            break_id=break_row["id"],
            match=match["id"],
            on=match["on"],
            platform=match["platform"],
            region=match["region"],
            exclusive=break_row["exclusive"],
            cost_minor=break_row["inventory_cost_minor"],
            available=True,
            blocked_reason=None,
        )
        blocked = self._blocked(contract, break_row, match)
        if blocked:
            forecast.available = False
            forecast.blocked_reason = blocked
            return forecast
        for other in self.contracts.values():
            if other["id"] == contract["id"] or other["category"] != contract["category"]:
                continue
            if break_row["exclusive"] and match["region"] in other["regions"] and match["platform"] in other["platforms"]:
                forecast.notes.append("同品类竞品可能争用该排他段，签约前需先裁定")
                break

        forecast.surveys_usable = any(
            row["match"] == match["id"] and row["surveyed_on"] <= self.as_of
            for row in self.panels
        )
        if not forecast.surveys_usable:
            forecast.evidence_gap = "survey_not_received"
            forecast.notes.append("该场次尚无到达的调查，只能保留缺证范围")
            return forecast
        if (match["region"], match["platform"]) not in self._historical_pairs(contract):
            forecast.evidence_gap = "no_comparable_history"
            forecast.notes.append("该地区或转播渠道没有可比历史，预测保留缺证范围")
        rows = self._survey_rows(contract, match)
        forecast.panel_rows = len(rows)
        if not rows:
            forecast.evidence_gap = "no_eligible_panel"
            forecast.notes.append("调查中没有符合合同地区与受众层的样本")
            return forecast
        on_region = [row for row in rows if row["region"] == match["region"]]
        if not on_region:
            forecast.evidence_gap = "survey_region_mismatch"
            forecast.notes.append("调查样本地区与场次主市场不一致，不能当作该场受众结构")
            return forecast
        if len(on_region) != len(rows):
            forecast.notes.append(
                f"{len(rows) - len(on_region)} 条调查属于合同允许但与场次主市场不同的地区，未用作本场结构"
            )
        rows = on_region

        calib = self.calibration(contract["id"])
        ratio = calib["coverage_ratio"] if calib["coverage_ratio"] is not None else 0.5
        forecast.calibration_ratio = max(0.0, min(1.0, ratio))
        forecast.avg_minutes = self._average_minutes(contract["id"])
        cap = contract["frequency_cap"]
        metric = contract["metric"]
        repeat_match = match["id"] in state["selected_matches"]
        # 受众调查只做去重，不直接当作触达率乘数；覆盖率来自事后校准。
        known_new = 0.0
        known_weight = 0.0
        unknown_new = 0.0
        unknown_weight = 0.0
        repeat_known = 0.0
        local_keys: set[tuple[str, str]] = set()
        for row in rows:
            if not row["attended"]:
                continue
            panel = row["panel_household"]
            known = row["known_household"]
            canonical = self.graph.canonical(known, match["on"]) if known else None
            key = ("h", canonical) if canonical else ("p", panel)
            if key in local_keys:
                continue
            local_keys.add(key)
            prior = state["exposure"].get(key, 0)
            already = (canonical in state["reached"]) if canonical else (key in state["reach_keys"])
            if already:
                forecast.already_reached += 1
                if metric == "view_minutes":
                    if prior < cap:
                        repeat_known += 1
                    else:
                        forecast.capped_households += 1
                continue
            if key in state["reach_keys"]:
                # 同场次或已安排场次已覆盖过该调查家庭
                if metric == "view_minutes":
                    if prior < cap:
                        repeat_known += 1
                    else:
                        forecast.capped_households += 1
                continue
            if metric == "view_minutes" and prior >= cap:
                forecast.capped_households += 1
                continue
            weight = 1.0 / row["inclusion_probability"]
            if canonical:
                known_new += 1
                known_weight += weight
            else:
                unknown_new += 1
                unknown_weight += weight
        if repeat_match:
            forecast.notes.append(
                "同场次追加广告段不新增触达家庭" if metric == "households"
                else "同场次追加广告段只增加重复曝光分钟，不新增触达家庭"
            )
        if forecast.evidence_gap == "no_comparable_history":
            forecast.notes.append("缺少可比历史，触达只作为缺证范围，不作为保证")
        forecast.known_new = float(known_new)
        forecast.known_weight = known_weight
        forecast.unknown_new = float(unknown_new)
        forecast.unknown_weight = unknown_weight
        # 上界只按已认定触达率与调查去重后的受众规模估计，不用每场调查样本
        # 直接乘出"每场新增家庭数"。
        forecast.notes.append(
            "上界按去重后的调查受众规模与已认定触达率估计，不是调查意愿的直接相加"
        )
        forecast.repeat_known = float(repeat_known)
        if forecast.capped_households:
            forecast.notes.append("部分已知家庭已到合同期频次上限，不再计新增收益")
        return forecast

    def _commit(self, contract: dict, break_row: dict, state: dict) -> None:
        match = self.matches[break_row["match"]]
        cap = contract["frequency_cap"]
        for row in self._survey_rows(contract, match):
            if not row["attended"] or row["region"] != match["region"]:
                continue
            known = row["known_household"]
            canonical = self.graph.canonical(known, match["on"]) if known else None
            key = ("h", canonical) if canonical else ("p", row["panel_household"])
            if contract["metric"] == "view_minutes":
                state["exposure"][key] = min(cap, state["exposure"].get(key, 0) + 1)
            elif known and canonical in state["reached"]:
                continue
            state["reach_keys"].add(key)
        state["selected_matches"].add(match["id"])

    # ---------- 计划 ----------

    def _candidate_breaks(self, contract: dict) -> list[dict]:
        rows = []
        for break_row in self.breaks.values():
            match = self.matches[break_row["match"]]
            if match["state"] != "scheduled" or match["on"] < self.as_of:
                continue
            rows.append(break_row)
        return sorted(rows, key=lambda b: (self.matches[b["match"]]["on"], b["start_seconds"], b["id"]))

    def plan(self, contract_id: str, level: str = "balanced") -> Plan:
        if level not in LEVELS:
            raise ValueError(f"未知保守程度: {level}")
        contract = self.contracts[contract_id]
        result = self.results[contract_id]
        metric = contract["metric"]
        gap_lower = result.gap_lower
        gap_upper = result.gap_upper
        state = self._fresh_state(contract_id)
        selected: list[dict] = []
        skipped: list[dict] = []
        spend = 0
        covered_lower = 0.0
        covered_upper = 0.0
        warned_competitor = False
        candidates = self._candidate_breaks(contract)
        for break_row in candidates:
            forecast = self.forecast_break(contract, break_row, state)
            if not forecast.available:
                skipped.append({**forecast.to_dict(), "skip_reason": forecast.blocked_reason})
                continue
            if covered_upper >= gap_upper and covered_lower >= gap_lower:
                skipped.append({**forecast.to_dict(), "skip_reason": "gap_covered"})
                continue
            if spend + forecast.cost_minor > contract["budget_minor"]:
                skipped.append({**forecast.to_dict(), "skip_reason": "budget_exhausted"})
                continue
            conservative = forecast.value(metric, "conservative")
            aggressive = forecast.value(metric, level)
            if aggressive <= 0:
                skipped.append({**forecast.to_dict(), "skip_reason": "no_incremental_gain"})
                continue
            selected.append(
                {
                    **forecast.to_dict(),
                    "level_value": round(forecast.value(metric, level), 3),
                    "conservative_value": round(conservative, 3),
                    "aggressive_value": round(forecast.value(metric, "aggressive"), 3),
                    "estimated_spend_minor": spend + forecast.cost_minor,
                }
            )
            spend += forecast.cost_minor
            covered_lower += conservative
            covered_upper += forecast.value(metric, level)
            self._commit(contract, break_row, state)
            if any("同品类竞品" in note for note in forecast.notes):
                warned_competitor = True
        remaining_lower = max(0.0, gap_lower - covered_lower)
        remaining_upper = max(0.0, gap_upper - covered_upper)
        warnings = []
        if remaining_lower > 0:
            warnings.append("保守口径下仍有缺口，不能承诺全部兑现")
        if remaining_upper > 0:
            warnings.append("即使按进取口径，仍有部分缺口无法用现有场次与预算保证")
        if any(item.get("evidence_gap") for item in selected):
            warnings.append("所选场次含缺可比历史或调查不足，范围仅作补量参考")
        if warned_competitor:
            warnings.append("存在同品类竞品争用排他段，签约前需先裁定")
        if any(item.get("blocked_reason") == "asset_failed" for item in skipped):
            warnings.append("存在平台回传上架失败的广告段，不能按已交付预计")
        return Plan(
            contract=contract,
            level=level,
            as_of=self.as_of,
            gap_lower=gap_lower,
            gap_upper=gap_upper,
            selected=selected,
            skipped=skipped,
            spend_minor=spend,
            budget_minor=contract["budget_minor"],
            covered_lower=covered_lower,
            covered_upper=covered_upper,
            remaining_lower=remaining_lower,
            remaining_upper=remaining_upper,
            unguaranteed=remaining_lower,
            warnings=warnings,
            calibration=self.calibration(contract_id),
        )

    def plan_portfolio(self, level: str = "balanced") -> list[Plan]:
        """按缺口从大到小排定，避免同一广告段被两家同时占用。

        已经被客户接受的广告段先保留；其余按缺口从大到小分配给各合同。
        """
        order = sorted(
            self.contracts,
            key=lambda cid: (
                -(self.results[cid].gap_upper),
                self.contracts[cid]["id"],
            ),
        )
        reserved = {b: set(v) for b, v in self.accepted_breaks.items()}
        plans = []
        for cid in order:
            self.accepted_breaks = reserved
            plan = self.plan(cid, level)
            plans.append(plan)
            for item in plan.selected:
                reserved.setdefault(item["break"], set()).add(cid)
        self.accepted_breaks = reserved
        return plans
