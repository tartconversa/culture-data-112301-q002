"""增量资料接收测试：后续场次回传进入对应合同期，迟到证据触发重核。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sponsor_ops.service import Service
from sponsor_ops.store import Ledger
from tests.fixtures import write_catalog
from tests.test_app import World, base_tables


class IncrementTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.batch_dir = tempfile.TemporaryDirectory()
        self.batch_root = Path(self.batch_dir.name)

    def tearDown(self):
        self.world.close()
        self.batch_dir.cleanup()

    def _batch(self, tables: dict[str, list[dict]]) -> None:
        write_catalog(self.batch_root, tables)

    def test_new_playback_enters_period_and_updates_gap(self):
        service = self.world.service()
        before = service.contract_report("C-A")["delivery"]["lower"]
        tables = base_tables()
        # M4 属于同一合同期；D3 已联系，但此前没有 M4 的播放
        tables["playback"] = [
            {"id": "N1", "origin_event": "NE1", "device": "D3", "match": "M4",
             "ad_break": "B4", "asset_version": "a-v1", "visible_seconds": 60,
             "played_seconds": 60, "covered": True, "background": False,
             "reported_on": "2026-09-05"},
        ]
        self._batch(tables)
        self.world.ledger.bootstrap()
        self.world.ledger.ingest_dir(self.batch_root)
        after = service.snapshot(refresh=True)
        refreshed = Service(self.world.ledger)
        report = refreshed.contract_report("C-A")
        self.assertGreater(report["delivery"]["lower"], before)
        self.assertEqual(service.snapshot().as_of, "2026-09-05")

    def test_out_of_period_playback_is_not_counted(self):
        service = self.world.service()
        before = service.contract_report("C-A")["delivery"]["lower"]
        tables = base_tables()
        tables["matches"] = [{
            "id": "M9", "on": "2026-10-02", "platform": "live-a", "region": "EU",
            "competition": "s3", "state": "completed",
        }]
        tables["ad_breaks"] = [{
            "id": "B9", "match": "M9", "start_seconds": 300, "duration_seconds": 60,
            "exclusive": True, "inventory_cost_minor": 5000, "accepted_contract": None,
        }]
        tables["playback"] = [{
            "id": "N2", "origin_event": "NE2", "device": "D3", "match": "M9",
            "ad_break": "B9", "asset_version": "a-v1", "visible_seconds": 60,
            "played_seconds": 60, "covered": True, "background": False,
            "reported_on": "2026-10-03",
        }]
        self._batch(tables)
        self.world.ledger.ingest_dir(self.batch_root)
        refreshed = Service(self.world.ledger)
        report = refreshed.contract_report("C-A")
        self.assertEqual(report["delivery"]["lower"], before)
        # 合同期外不算交付，但要单独解释为"合同期外"
        self.assertEqual(report["delivery"]["by_reason"].get("period", 0), 1)

    def test_visibility_revision_can_remove_a_credited_unit(self):
        service = self.world.service()
        before = service.contract_report("C-A")["delivery"]["lower"]
        service.add_evidence([
            {"id": "V1", "kind": "visibility_revision", "subject": "E1",
             "received_on": "2026-09-06", "payload": {"visible_seconds": 10, "covered": True}},
        ])
        service.as_of = "2026-09-06"
        after = service.snapshot(refresh=True)
        report = service.contract_report("C-A")
        self.assertLessEqual(report["delivery"]["lower"], before)
        self.assertGreater(report["delivery"]["by_reason"].get("short", 0), 0)

    def test_logical_bad_batch_is_rejected_without_partial_write(self):
        tables = base_tables()
        tables["playback"] = [{
            "id": "bad", "origin_event": "bad", "device": "D-missing", "match": "M1",
            "ad_break": "B1", "asset_version": "a-v1", "visible_seconds": 60,
            "played_seconds": 60, "covered": True, "background": False,
            "reported_on": "2026-09-07",
        }]
        with self.assertRaises(ValueError):
            self.world.ledger.ingest_table("playback", tables["playback"], "manual")
        service = Service(self.world.ledger)
        self.assertEqual(service.contract_report("C-A")["delivery"]["lower"], 1)


if __name__ == "__main__":
    unittest.main()
