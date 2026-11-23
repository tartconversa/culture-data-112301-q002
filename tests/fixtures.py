"""电竞直播赞助履约测试使用的最小合成资料。

所有记录均为虚构，字段与 data/catalog.json 契约一致。测试只通过临时目录
载入，不修改 data 下的原始资料。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sponsor_ops.materials import MaterialError


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_catalog(directory: Path, tables: dict[str, list[dict]]) -> None:
    path = directory / "data"
    path.mkdir(parents=True, exist_ok=True)
    catalog = {"format": "jsonl-utf8-v1", "synthetic": True, "tables": {}}
    schemas = SCHEMAS
    for name, rows in tables.items():
        file = path / f"{name}.jsonl"
        file.write_text(
            "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        spec = {
            "description": schemas[name]["description"],
            "fields": schemas[name]["fields"],
            "key": schemas[name]["key"],
            "references": schemas[name].get("references", {}),
            "sha256": _hash(file),
            "row_count": len(rows),
        }
        catalog["tables"][name] = spec
    (path / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _field(kind: str, description: str, nullable: bool = False) -> dict:
    return {"type": kind, "nullable": nullable, "description": description}


SCHEMAS = {
    "devices": {
        "description": "匿名观众设备",
        "key": "id",
        "fields": {
            "id": _field("str", "设备"),
            "region": _field("str", "地区"),
            "segment": _field("str", "受众层"),
            "consent": _field("bool", "是否同意计量"),
        },
    },
    "household_links": {
        "description": "设备与家庭联系",
        "key": "id",
        "fields": {
            "id": _field("str", "证据"),
            "devices": _field("list", "设备"),
            "household": _field("str", "家庭"),
            "status": _field("str", "confirmed/possible"),
            "effective_from": _field("str", "生效日"),
            "received_on": _field("str", "到达日"),
        },
    },
    "matches": {
        "description": "场次",
        "key": "id",
        "fields": {
            "id": _field("str", "场次"),
            "on": _field("str", "直播日"),
            "platform": _field("str", "转播平台"),
            "region": _field("str", "主市场"),
            "competition": _field("str", "赛事季"),
            "state": _field("str", "completed/scheduled"),
        },
    },
    "ad_breaks": {
        "description": "广告段",
        "key": "id",
        "references": {"match": ["matches", "id"]},
        "fields": {
            "id": _field("str", "广告段"),
            "match": _field("str", "场次"),
            "start_seconds": _field("int", "起始秒"),
            "duration_seconds": _field("int", "长度秒"),
            "exclusive": _field("bool", "是否排他"),
            "inventory_cost_minor": _field("int", "采购成本美分"),
            "accepted_contract": _field("str", "已承诺合同", True),
        },
    },
    "sponsorships": {
        "description": "赞助合同",
        "key": "id",
        "fields": {
            "id": _field("str", "合同"),
            "advertiser": _field("str", "广告主"),
            "category": _field("str", "品类"),
            "metric": _field("str", "households/view_minutes"),
            "target": _field("float", "目标"),
            "unit_price_minor": _field("int", "单价"),
            "frequency_cap": _field("int", "频次上限"),
            "regions": _field("list", "地区"),
            "segments": _field("list", "受众层"),
            "platforms": _field("list", "渠道"),
            "asset_versions": _field("list", "素材版本"),
            "allow_parallel_credit": _field("bool", "允许并计"),
            "splittable": _field("bool", "可拆分"),
            "budget_minor": _field("int", "补量预算"),
            "period_from": _field("str", "合同期起"),
            "period_to": _field("str", "合同期末"),
        },
    },
    "playback": {
        "description": "播放回传",
        "key": "id",
        "references": {
            "device": ["devices", "id"],
            "match": ["matches", "id"],
            "ad_break": ["ad_breaks", "id"],
        },
        "fields": {
            "id": _field("str", "回传"),
            "origin_event": _field("str", "原始事件"),
            "device": _field("str", "设备"),
            "match": _field("str", "场次"),
            "ad_break": _field("str", "广告段"),
            "asset_version": _field("str", "素材版本"),
            "visible_seconds": _field("int", "可见秒"),
            "played_seconds": _field("int", "播放秒"),
            "covered": _field("bool", "有监测"),
            "background": _field("bool", "后台"),
            "reported_on": _field("str", "到达日"),
        },
    },
    "audience_panels": {
        "description": "受众调查",
        "key": "id",
        "references": {
            "match": ["matches", "id"],
            "known_household": ["household_links", "household"],
        },
        "fields": {
            "id": _field("str", "调查"),
            "match": _field("str", "场次"),
            "panel_household": _field("str", "调查家庭"),
            "known_household": _field("str", "已知家庭", True),
            "region": _field("str", "地区"),
            "segment": _field("str", "受众层"),
            "inclusion_probability": _field("float", "纳入概率"),
            "attended": _field("bool", "观看"),
            "surveyed_on": _field("str", "调查日"),
        },
    },
    "recognized_delivery": {
        "description": "商务认定",
        "key": "id",
        "references": {"contract": ["sponsorships", "id"]},
        "fields": {
            "id": _field("str", "认定"),
            "origin_event": _field("str", "事件"),
            "contract": _field("str", "合同"),
            "recognized_on": _field("str", "认定日"),
            "credited_units": _field("float", "认定单位"),
            "recognition_version": _field("int", "版本"),
        },
    },
    "invoices": {
        "description": "账单",
        "key": "id",
        "references": {"contract": ["sponsorships", "id"]},
        "fields": {
            "id": _field("str", "账单"),
            "contract": _field("str", "合同"),
            "recognitions": _field("list", "认定"),
            "amount_minor": _field("int", "票面"),
            "issued_on": _field("str", "开票日"),
            "paid_minor": _field("int", "已收款"),
        },
    },
    "makegood_requests": {
        "description": "补量申请",
        "key": "id",
        "references": {"contract": ["sponsorships", "id"]},
        "fields": {
            "id": _field("str", "申请"),
            "contract": _field("str", "合同"),
            "ad_breaks": _field("list", "候选广告段"),
            "requested_on": _field("str", "申请日"),
            "accepted_breaks": _field("list", "已接受"),
            "all_or_none": _field("bool", "整体落实"),
        },
    },
    "later_evidence": {
        "description": "后续资料",
        "key": "id",
        "fields": {
            "id": _field("str", "资料"),
            "kind": _field("str", "类别"),
            "subject": _field("str", "对象"),
            "received_on": _field("str", "到达日"),
            "payload": _field("dict", "内容"),
        },
    },
}
