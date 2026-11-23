"""持久化账本：SQLite 保存接收批次、原始记录和人工确认。

关键点：
- 原始资料文件只读；每次接收把记录写入 ingest_rows，保留 source/line。
- 同一主键的资料保留每次到达的原始行；查询时取最新版本，历史版本仍可审计。
- 商务决定（补量接受、账款调整）也落库，形成可追溯的本地状态。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .materials import (
    MaterialError,
    load_catalog,
    merge_tables,
    read_tables,
    seed_tables,
    validate_tables,
    validate_rows,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    source TEXT NOT NULL,
    tables TEXT NOT NULL,
    row_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ingest_rows (
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    table_name TEXT NOT NULL,
    row_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    source TEXT NOT NULL,
    line_no INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ingest_rows_table_key ON ingest_rows(table_name, row_key);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    subject TEXT NOT NULL,
    payload TEXT NOT NULL,
    decided_on TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_topic_subject ON decisions(topic, subject);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Ledger:
    def __init__(self, root: Path, db_path: Path | None = None):
        self.root = Path(root)
        self.catalog = load_catalog(self.root)
        self.path = Path(db_path) if db_path else self.root / "state" / "ledger.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------- 接收 ----------

    def bootstrap(self) -> int:
        """首次接收原始资料；已接收过则保持幂等。"""
        if self._has_seed():
            return 0
        tables = seed_tables(self.root, self.catalog)
        return self._insert_batch(tables, source="seed", root=self.root)

    def _has_seed(self) -> bool:
        return self.conn.execute("SELECT 1 FROM batches LIMIT 1").fetchone() is not None


    def ingest_dir(self, directory: Path) -> dict[str, Any]:
        """接收一个含 catalog.json 和 jsonl 的资料目录（外部批次）。

        批次内跨表引用可以指向既有账本记录；合并后整份资料必须仍然自洽，
        校验失败时不写入任何记录。
        """
        directory = Path(directory)
        catalog = load_catalog(directory)
        updates = read_tables(directory, catalog, check_references=False)
        merged = merge_tables(self.all_tables(), updates, catalog)
        validate_tables(merged, catalog)
        return {
            "batch": self._insert_batch(updates, source=str(directory), root=directory),
            "tables": {name: len(rows) for name, rows in updates.items()},
            "merged": {name: len(rows) for name, rows in merged.items()},
        }

    def ingest_table(self, name: str, rows: list[dict], source: str) -> dict[str, Any]:
        if name not in self.catalog["tables"]:
            raise MaterialError(f"未知资料表: {name}")
        validate_rows(name, rows, self.catalog, None)
        merged = merge_tables(self.all_tables(), {name: rows}, self.catalog)
        validate_tables(merged, self.catalog)
        batch = self._insert_batch({name: rows}, source=source, root=None)
        return {"batch": batch, "table": name, "rows": len(rows)}

    def _insert_batch(
        self, tables: dict[str, list[dict]], source: str, root: Path | None
    ) -> int:
        payload = {name: len(rows) for name, rows in tables.items()}
        cur = self.conn.execute(
            "INSERT INTO batches(received_at, source, tables, row_count) VALUES (?,?,?,?)",
            (utc_now(), source, json.dumps(payload, ensure_ascii=False), sum(payload.values())),
        )
        batch_id = int(cur.lastrowid)
        for name, rows in tables.items():
            key = self.catalog["tables"][name]["key"]
            for index, row in enumerate(rows):
                self.conn.execute(
                    "INSERT INTO ingest_rows(batch_id, table_name, row_key, payload, source, line_no)"
                    " VALUES (?,?,?,?,?,?)",
                    (
                        batch_id,
                        name,
                        str(row[key]),
                        json.dumps(row, ensure_ascii=False, sort_keys=True),
                        source,
                        index + 1,
                    ),
                )
        self.conn.commit()
        return batch_id

    # ---------- 读取 ----------

    def batches(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM batches ORDER BY id")]

    def table(self, name: str) -> list[dict]:
        """按业务主键取每个资料键的最新收到版本。

        种子先于外部批次；外部批次同主键视为对同一业务对象的更新。
        同一批次内主键重复不会被静默覆盖，接收时已校验拒绝。
        """
        rows: dict[str, dict] = {}
        for record in self.conn.execute(
            "SELECT row_key, payload FROM ingest_rows WHERE table_name = ? ORDER BY batch_id, line_no",
            (name,),
        ):
            rows[record["row_key"]] = json.loads(record["payload"])
        return list(rows.values())

    def all_tables(self) -> dict[str, list[dict]]:
        return {name: self.table(name) for name in self.catalog["tables"]}

    def row_versions(self, name: str, key: str) -> list[dict]:
        return [
            {
                "batch": record["batch_id"],
                "line": record["line_no"],
                "source": record["source"],
                "row": json.loads(record["payload"]),
            }
            for record in self.conn.execute(
                "SELECT batch_id, line_no, source, payload FROM ingest_rows"
                " WHERE table_name = ? AND row_key = ? ORDER BY batch_id, line_no",
                (name, key),
            )
        ]

    # ---------- 人工决定 ----------

    def decide(self, topic: str, subject: str, payload: dict, decided_on: str) -> int:
        now = utc_now()
        cur = self.conn.execute(
            "INSERT INTO decisions(topic, subject, payload, decided_on, recorded_at) VALUES (?,?,?,?,?)",
            (topic, subject, json.dumps(payload, ensure_ascii=False, sort_keys=True), decided_on, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def decisions(self, topic: str | None = None, subject: str | None = None) -> list[dict]:
        sql = "SELECT * FROM decisions"
        clauses = []
        args: list[Any] = []
        if topic is not None:
            clauses.append("topic = ?")
            args.append(topic)
        if subject is not None:
            clauses.append("subject = ?")
            args.append(subject)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        return [
            {
                "id": row["id"],
                "topic": row["topic"],
                "subject": row["subject"],
                "payload": json.loads(row["payload"]),
                "decided_on": row["decided_on"],
                "recorded_at": row["recorded_at"],
            }
            for row in self.conn.execute(sql, args)
        ]
