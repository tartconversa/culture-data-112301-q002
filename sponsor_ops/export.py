"""合同交付、补量与账款的只读导出。

导出文件默认写入 outputs/，可用于商务评审或留档。导出结果只反映当前
账本与截止日，不修改任何原始资料或商务认定。
"""
from __future__ import annotations

import json
from pathlib import Path

from .service import Service


def export_all(service: Service, destination: Path) -> dict[str, str]:
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    snap = service.snapshot()
    contracts = {
        row["id"]: service.contract_report(row["id"]) for row in snap.tables["sponsorships"]
    }
    plan_levels = {}
    for level in ("conservative", "balanced", "aggressive"):
        plan_levels[level] = [plan.to_dict() for plan in service.plans(level)]
    payloads = {
        "contracts.json": contracts,
        "plans.json": plan_levels,
        "playback.json": {
            "as_of": snap.as_of,
            "raw": snap.raw_totals,
            "dedupe": {
                "mirror_rows_collapsed": snap.playback.mirrored_rows,
                "repeated_callback_units_collapsed": snap.playback.duplicate_callbacks,
                "units_after_dedupe": len(snap.playback.units),
                "conflicting_callback_units": snap.playback.conflict_units,
            },
            "notes": snap.notes,
        },
    }
    for name, payload in payloads.items():
        path = destination / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        written[name] = str(path)
    return written
