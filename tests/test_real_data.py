"""资料接收、触达解释、交付认定、补量接受与账款差额的持续使用入口。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sponsor_ops.export import export_all
from sponsor_ops.service import Service
from sponsor_ops.store import Ledger

ROOT = Path(__file__).resolve().parents[1]


class RealDataTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(ROOT, ROOT / "state" / "test-ledger.sqlite3")
        self.ledger.bootstrap()
        self.service = Service(self.ledger)

    def tearDown(self):
        self.ledger.close()
        (ROOT / "state" / "test-ledger.sqlite3").unlink(missing_ok=True)

    def test_raw_qualified_recognized_are_distinct(self):
        report = self.service.contract_report("C-drink-a")
        self.assertGreater(report["raw"]["raw_rows"], report["raw"]["origin_events"])
        self.assertEqual(report["dedupe"]["units_after_dedupe"], 1800)
        self.assertGreater(report["delivery"]["lower"], 0)
        self.assertLess(report["delivery"]["lower"], report["delivery"]["upper"])
        support = report["recognitions"]
        self.assertLess(support["supported_units"], support["claimed_units"])

    def test_late_evidence_changes_gap_but_keeps_invoice(self):
        before = Service(self.ledger, as_of="2026-08-27").contract_report("C-drink-a")
        after = Service(self.ledger, as_of="2026-08-30").contract_report("C-drink-a")
        self.assertGreaterEqual(before["delivery"]["lower"], after["delivery"]["lower"])
        self.assertGreater(after["invoices"][0]["variance_minor"], before["invoices"][0]["variance_minor"])
        self.assertEqual(after["invoices"][0]["paid_minor"], 120000)

    def test_plans_do_not_double_assign_breaks(self):
        plans = self.service.plans("balanced")
        seen: dict[str, str] = {}
        for plan in plans:
            for item in plan.selected:
                self.assertNotIn(item["break"], seen, item["break"])
                seen[item["break"]] = plan.contract["id"]
        self.assertGreater(len(seen), 0)

    def test_export_writes_read_only_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            written = export_all(self.service, Path(directory))
            self.assertEqual(set(written), {"contracts.json", "plans.json", "playback.json"})
            payload = json.loads((Path(directory) / "contracts.json").read_text(encoding="utf-8"))
            self.assertIn("C-drink-a", payload)
            self.assertEqual(payload["C-drink-a"]["invoices"][0]["paid_minor"], 120000)

    def test_asset_receipt_blocks_break(self):
        report = self.service.ad_break_report("B138")
        self.assertEqual(report["failed_contracts"], ["C-drink-b"])
        self.assertEqual(report["accepted_contracts"], [])

    def test_unknown_ids_raise(self):
        with self.assertRaises(KeyError):
            self.service.contract_report("C-nope")
        with self.assertRaises(KeyError):
            self.service.ad_break_report("B-nope")
        with self.assertRaises(KeyError):
            self.service.invoice_report("INV-nope")


if __name__ == "__main__":
    unittest.main()
