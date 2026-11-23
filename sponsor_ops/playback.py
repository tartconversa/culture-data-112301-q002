"""把原始回传整理成可计量的播放单元。

规则：
- 同一 origin_event 的镜像回传只保留一次。
- 同一 (device, match, ad_break) 的不同 origin_event 属于同一次播放的重复回传，
  只形成一个播放单元，不能算作两次曝光。
- 重复回传之间若可见性不一致，单元给出资料支持的可见秒数区间
  [lower, upper]：lower 取合格回传中的最小值，upper 取最大值。合格指
  有监测、非后台且可见至少 30 秒。
- 若所有回传都不合格，单元被排除，并按最接近合格的原因归类，后台播放、
  缺监测与可见不足分别计数，不互相掩盖。
- 迟到可见性更正按收到日生效，只覆盖被点名的播放事件。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable

MIN_VISIBLE_SECONDS = 30
EXCLUSION_LABELS = {
    "mirror": "平台镜像回传",
    "background": "后台播放",
    "uncovered": "缺可见性监测",
    "short": "可见不足30秒",
}


@dataclass
class PlayUnit:
    """一个 (设备, 场次, 广告段) 的播放单元。"""

    device: str
    match: str
    ad_break: str
    asset_version: str
    visible_lower: float
    visible_upper: float
    covered: bool
    background: bool
    reported_on: str
    origin_events: list[str] = field(default_factory=list)
    callbacks: int = 1
    revision: dict | None = None
    conflict: bool = False
    exclusion: str | None = None

    @property
    def visible_seconds(self) -> float:
        """兼容单值读取：取资料能支持的上限。"""
        return self.visible_upper

    def quality_issue(self) -> str | None:
        return self.exclusion

    def to_dict(self) -> dict:
        return {
            "device": self.device,
            "match": self.match,
            "ad_break": self.ad_break,
            "asset_version": self.asset_version,
            "visible_lower": round(self.visible_lower, 3),
            "visible_upper": round(self.visible_upper, 3),
            "covered": self.covered,
            "background": self.background,
            "reported_on": self.reported_on,
            "origin_events": list(self.origin_events),
            "callbacks": self.callbacks,
            "revision": self.revision,
            "conflict": self.conflict,
            "quality_issue": self.exclusion,
        }


@dataclass
class PlaybackResult:
    units: list[PlayUnit]
    mirrored_rows: int
    duplicate_callbacks: int
    revisions_applied: int
    revision_subjects: list[str]
    conflict_units: int = 0
    excluded_units: int = 0

    def by_origin(self) -> dict[str, PlayUnit]:
        return {origin: unit for unit in self.units for origin in unit.origin_events}


def _issue_for(row: dict) -> str | None:
    if row["background"]:
        return "background"
    if not row["covered"]:
        return "uncovered"
    if row["visible_seconds"] < MIN_VISIBLE_SECONDS:
        return "short"
    return None


def _unit_exclusion(rows: list[dict]) -> str | None:
    """没有合格回传时，取最接近合格的单一原因。"""
    issues = [_issue_for(row) for row in rows]
    if "short" in issues:
        return "short"
    if "uncovered" in issues:
        return "uncovered"
    if "background" in issues:
        return "background"
    return "uncovered"


def build_play_units(
    playback_rows: Iterable[dict],
    evidence: Iterable[dict],
    as_of: str | None = None,
) -> PlaybackResult:
    revisions: dict[str, dict] = {}
    revision_subjects: list[str] = []
    for item in evidence:
        if item.get("kind") != "visibility_revision":
            continue
        if as_of is not None and item["received_on"] > as_of:
            continue
        revisions[item["subject"]] = item["payload"]
        revision_subjects.append(item["subject"])

    seen_origin: dict[str, dict] = {}
    mirrored = 0
    for row in playback_rows:
        if as_of is not None and row["reported_on"] > as_of:
            continue
        if row["origin_event"] in seen_origin:
            mirrored += 1
            continue
        seen_origin[row["origin_event"]] = dict(row)

    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in seen_origin.values():
        row = dict(row)
        payload = revisions.get(row["origin_event"])
        if payload:
            row.update(payload)
        groups[(row["device"], row["match"], row["ad_break"])].append(row)

    units: list[PlayUnit] = []
    duplicate_callbacks = 0
    conflict = 0
    applied = sum(1 for origin in revisions if origin in seen_origin)
    for (device, match, ad_break), rows in sorted(groups.items()):
        rows.sort(key=lambda r: (r["reported_on"], r["origin_event"]))
        valid = [r for r in rows if _issue_for(r) is None]
        if len(rows) > 1:
            duplicate_callbacks += len(rows) - 1
        revisions_here = [
            {"origin_event": r["origin_event"], **revisions[r["origin_event"]]}
            for r in rows
            if r["origin_event"] in revisions
        ]
        if valid:
            lower = float(min(r["visible_seconds"] for r in valid))
            upper = float(max(r["visible_seconds"] for r in valid))
            exclusion = None
        else:
            lower = upper = 0.0
            exclusion = _unit_exclusion(rows)
        has_conflict = len({(r["visible_seconds"], r["covered"], r["background"]) for r in rows}) > 1
        if has_conflict:
            conflict += 1
        units.append(
            PlayUnit(
                device=device,
                match=match,
                ad_break=ad_break,
                asset_version=rows[0]["asset_version"],
                visible_lower=lower,
                visible_upper=upper,
                covered=bool(valid),
                background=all(r["background"] for r in rows),
                reported_on=min(r["reported_on"] for r in rows),
                origin_events=sorted(r["origin_event"] for r in rows),
                callbacks=len(rows),
                revision=revisions_here[0] if revisions_here else None,
                conflict=has_conflict,
                exclusion=exclusion,
            )
        )
    return PlaybackResult(
        units=units,
        mirrored_rows=mirrored,
        duplicate_callbacks=duplicate_callbacks,
        revisions_applied=applied,
        revision_subjects=sorted(revision_subjects),
        conflict_units=conflict,
        excluded_units=sum(1 for unit in units if unit.exclusion),
    )


def exclusion_counts(playback: PlaybackResult) -> Counter:
    counts: Counter = Counter()
    for unit in playback.units:
        if unit.exclusion:
            counts[EXCLUSION_LABELS[unit.exclusion]] += 1
    return counts


def raw_callback_totals(playback_rows: list[dict]) -> dict:
    rows = playback_rows
    return {
        "raw_rows": len(rows),
        "origin_events": len({r["origin_event"] for r in rows}),
        "raw_visible_seconds": sum(r["visible_seconds"] for r in rows),
        "covered_rows": sum(1 for r in rows if r["covered"]),
        "background_rows": sum(1 for r in rows if r["background"]),
    }
