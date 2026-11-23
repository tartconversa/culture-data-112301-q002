"""合成资料的读取、校验与增量接收。

资料文件本身只读。应用把收到过的记录保存在 state 下的 SQLite 中，后续查询
都基于这份可追加的本地账本；这样迟到的回传或关联证据不会改写原始文件。
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


class MaterialError(ValueError):
    """资料格式、指纹或直接引用不符合契约。"""


TYPE_NAMES = {
    "str": str,
    "int": int,
    "float": (int, float),
    "bool": bool,
    "list": list,
    "dict": dict,
}


def load_catalog(root: Path) -> dict:
    path = Path(root) / "data" / "catalog.json"
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:  # pragma: no cover - 调用方目录错误
        raise MaterialError(f"找不到资料目录: {path}") from exc
    if not isinstance(catalog.get("tables"), dict):
        raise MaterialError("catalog.json 缺少 tables")
    return catalog


def read_seed_table(root: Path, name: str, spec: dict) -> list[dict]:
    path = Path(root) / "data" / f"{name}.jsonl"
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != spec["sha256"]:
        raise MaterialError(f"资料指纹不符: {name}")
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    if len(rows) != spec["row_count"]:
        raise MaterialError(f"资料行数不符: {name}")
    return rows


def validate_rows(
    name: str,
    rows: Iterable[dict],
    catalog: dict,
    lookup: dict[str, set[Any]] | None = None,
) -> None:
    """检查一套已合并的记录，不依赖逐行 id 唯一之外的文件身份。"""
    if name not in catalog["tables"]:
        raise MaterialError(f"未知资料表: {name}")
    spec = catalog["tables"][name]
    fields = spec["fields"]
    keys = set()
    for row in rows:
        if not isinstance(row, dict):
            raise MaterialError(f"{name} 记录不是对象")
        if set(row) != set(fields):
            raise MaterialError(f"{name} 字段不符")
        for field, definition in fields.items():
            value = row[field]
            if value is None:
                if not definition["nullable"]:
                    raise MaterialError(f"{name}.{field} 不可为空")
                continue
            expected = TYPE_NAMES[definition["type"]]
            # bool 是 int 的子类，需要单独排除
            if definition["type"] == "int" and isinstance(value, bool):
                raise MaterialError(f"{name}.{field} 应为整数")
            if definition["type"] == "bool" and not isinstance(value, bool):
                raise MaterialError(f"{name}.{field} 应为布尔")
            if not isinstance(value, expected):
                raise MaterialError(f"{name}.{field} 类型不符")
        key = row[spec["key"]]
        if key in keys:
            raise MaterialError(f"{name} 主键重复: {key}")
        keys.add(key)
    if lookup is None:
        return
    for field, (target, target_key) in spec["references"].items():
        allowed = lookup.get(target)
        if allowed is None:
            continue
        for row in rows:
            if row[field] is not None and row[field] not in allowed:
                raise MaterialError(f"{name}.{field} 直接引用不存在: {row[field]}")


def build_lookup(catalog: dict, tables: dict[str, list[dict]]) -> dict[str, set[Any]]:
    """引用目标既可以是主键，也可以是契约点名的业务字段。"""
    targets: dict[str, set[str]] = defaultdict(set)
    for spec in catalog["tables"].values():
        for target, target_field in spec.get("references", {}).values():
            targets[target].add(target_field)
    lookup: dict[str, set[Any]] = {}
    for name, spec in catalog["tables"].items():
        rows = tables.get(name, [])
        values = {row[spec["key"]] for row in rows}
        for target_field in targets.get(name, set()):
            if rows and target_field in rows[0]:
                values |= {row[target_field] for row in rows}
        lookup[name] = values
    return lookup


def read_tables(root: Path, catalog: dict, check_references: bool = True) -> dict[str, list[dict]]:
    tables = {}
    for name, spec in catalog["tables"].items():
        tables[name] = read_seed_table(root, name, spec)
        validate_rows(name, tables[name], catalog, None)
    if check_references:
        lookup = build_lookup(catalog, tables)
        for name, rows in tables.items():
            validate_rows(name, rows, catalog, lookup)
    return tables


def seed_tables(root: Path, catalog: dict | None = None) -> dict[str, list[dict]]:
    return read_tables(root, catalog or load_catalog(root))


def merge_tables(
    base: dict[str, list[dict]], updates: dict[str, list[dict]], catalog: dict
) -> dict[str, list[dict]]:
    """按业务主键合并增量记录；后者覆盖前者。"""
    merged = {}
    for name, spec in catalog["tables"].items():
        key = spec["key"]
        rows = {row[key]: row for row in base.get(name, [])}
        for row in updates.get(name, []):
            rows[row[key]] = row
        merged[name] = list(rows.values())
    return merged


def validate_tables(tables: dict[str, list[dict]], catalog: dict) -> None:
    lookup = build_lookup(catalog, tables)
    for name, rows in tables.items():
        validate_rows(name, rows, catalog, lookup)
