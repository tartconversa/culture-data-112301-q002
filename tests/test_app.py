"""端到端测试：迟到家庭关联、已认定交付、竞品补量争用。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sponsor_ops.identity import HouseholdGraph
from sponsor_ops.materials import MaterialError, seed_tables
from sponsor_ops.playback import build_play_units
from sponsor_ops.service import Service
from sponsor_ops.store import Ledger
from tests.fixtures import write_catalog


def base_tables() -> dict[str, list[dict]]:
    return {
        "devices": [],
        "household_links": [],
        "matches": [],
        "ad_breaks": [],
        "sponsorships": [],
        "playback": [],
        "audience_panels": [],
        "recognized_delivery": [],
        "invoices": [],
        "makegood_requests": [],
        "later_evidence": [],
    }


def standard_world() -> dict[str, list[dict]]:
    tables = base_tables()
    tables["devices"] = [
        {"id": "D1", "region": "EU", "segment": "adult", "consent": True},
        {"id": "D2", "region": "EU", "segment": "adult", "consent": True},
        {"id": "D3", "region": "EU", "segment": "adult", "consent": True},
        {"id": "D4", "region": "EU", "segment": "adult", "consent": True},
        {"id": "D5", "region": "EU", "segment": "adult", "consent": True},
    ]
    tables["household_links"] = [
        {"id": "H1", "devices": ["D1"], "household": "HH1", "status": "confirmed",
         "effective_from": "2026-06-01", "received_on": "2026-06-01"},
        {"id": "H2", "devices": ["D3"], "household": "HH3", "status": "confirmed",
         "effective_from": "2026-06-01", "received_on": "2026-06-01"},
        {"id": "H3", "devices": ["D4"], "household": "HH4", "status": "possible",
         "effective_from": "2026-06-01", "received_on": "2026-06-01"},
        {"id": "H4", "devices": ["D2"], "household": "HH2", "status": "confirmed",
         "effective_from": "2026-06-01", "received_on": "2026-06-01"},
    ]
    tables["matches"] = [
        {"id": "M1", "on": "2026-06-02", "platform": "live-a", "region": "EU",
         "competition": "s1", "state": "completed"},
        {"id": "M2", "on": "2026-06-04", "platform": "live-a", "region": "EU",
         "competition": "s1", "state": "completed"},
        {"id": "M5", "on": "2026-06-06", "platform": "live-a", "region": "EU",
         "competition": "s1", "state": "completed"},
        {"id": "M3", "on": "2026-09-02", "platform": "live-a", "region": "EU",
         "competition": "s2", "state": "scheduled"},
        {"id": "M4", "on": "2026-09-04", "platform": "live-a", "region": "EU",
         "competition": "s2", "state": "scheduled"},
    ]
    tables["ad_breaks"] = [
        {"id": "B1", "match": "M1", "start_seconds": 300, "duration_seconds": 60,
         "exclusive": True, "inventory_cost_minor": 5000, "accepted_contract": None},
        {"id": "B2", "match": "M2", "start_seconds": 300, "duration_seconds": 60,
         "exclusive": True, "inventory_cost_minor": 5000, "accepted_contract": None},
        {"id": "B3", "match": "M3", "start_seconds": 300, "duration_seconds": 60,
         "exclusive": True, "inventory_cost_minor": 5000, "accepted_contract": None},
        {"id": "B4", "match": "M4", "start_seconds": 300, "duration_seconds": 60,
         "exclusive": True, "inventory_cost_minor": 5000, "accepted_contract": None},
        {"id": "B5", "match": "M5", "start_seconds": 300, "duration_seconds": 60,
         "exclusive": True, "inventory_cost_minor": 5000, "accepted_contract": None},
    ]
    tables["sponsorships"] = [
        {
            "id": "C-A", "advertiser": "brand-a", "category": "drink",
            "metric": "households", "target": 4.0, "unit_price_minor": 1000,
            "frequency_cap": 3, "regions": ["EU"], "segments": ["adult"],
            "platforms": ["live-a"], "asset_versions": ["a-v1"],
            "allow_parallel_credit": False, "splittable": True,
            "budget_minor": 20000, "period_from": "2026-06-01", "period_to": "2026-09-30",
        },
        {
            "id": "C-B", "advertiser": "brand-b", "category": "drink",
            "metric": "households", "target": 3.0, "unit_price_minor": 1000,
            "frequency_cap": 3, "regions": ["EU"], "segments": ["adult"],
            "platforms": ["live-a"], "asset_versions": ["b-v1"],
            "allow_parallel_credit": False, "splittable": False,
            "budget_minor": 20000, "period_from": "2026-06-01", "period_to": "2026-09-30",
        },
    ]
    tables["playback"] = [
        # D1 与 D2 是同一个家庭 HH1，但迟到证据在认定后才到
        {"id": "P1", "origin_event": "E1", "device": "D1", "match": "M1",
         "ad_break": "B1", "asset_version": "a-v1", "visible_seconds": 60,
         "played_seconds": 60, "covered": True, "background": False,
         "reported_on": "2026-06-03"},
        {"id": "P2", "origin_event": "E2", "device": "D2", "match": "M2",
         "ad_break": "B2", "asset_version": "a-v1", "visible_seconds": 60,
         "played_seconds": 60, "covered": True, "background": False,
         "reported_on": "2026-06-05"},
        # 竞品在同一排他段 B5 上的播放
        {"id": "P3", "origin_event": "E3", "device": "D3", "match": "M5",
         "ad_break": "B5", "asset_version": "b-v1", "visible_seconds": 60,
         "played_seconds": 60, "covered": True, "background": False,
         "reported_on": "2026-06-07"},
        # 同一排他段上另一方合同也主张，归属未定
        {"id": "P8", "origin_event": "E8", "device": "D1", "match": "M5",
         "ad_break": "B5", "asset_version": "a-v1", "visible_seconds": 60,
         "played_seconds": 60, "covered": True, "background": False,
         "reported_on": "2026-06-07"},
        # 后台播放，不能计入
        {"id": "P4", "origin_event": "E4", "device": "D3", "match": "M1",
         "ad_break": "B1", "asset_version": "a-v1", "visible_seconds": 55,
         "played_seconds": 60, "covered": True, "background": True,
         "reported_on": "2026-06-03"},
        # 重复回传：同一设备场次广告段的两条回传，只能算一次播放
        {"id": "P5", "origin_event": "E5", "device": "D4", "match": "M1",
         "ad_break": "B1", "asset_version": "a-v1", "visible_seconds": 40,
         "played_seconds": 60, "covered": True, "background": False,
         "reported_on": "2026-06-03"},
        {"id": "P6", "origin_event": "E6", "device": "D4", "match": "M1",
         "ad_break": "B1", "asset_version": "a-v1", "visible_seconds": 50,
         "played_seconds": 60, "covered": True, "background": False,
         "reported_on": "2026-06-03"},
        # 无任何联系证据的设备
        {"id": "P7", "origin_event": "E7", "device": "D5", "match": "M1",
         "ad_break": "B1", "asset_version": "a-v1", "visible_seconds": 60,
         "played_seconds": 60, "covered": True, "background": False,
         "reported_on": "2026-06-03"},
    ]
    tables["audience_panels"] = [
        # M3 调查：HH1 是已触达家庭；PH9 无联系身份
        {"id": "S1", "match": "M3", "panel_household": "PH1", "known_household": "HH1",
         "region": "EU", "segment": "adult", "inclusion_probability": 0.5,
         "attended": True, "surveyed_on": "2026-08-25"},
        {"id": "S2", "match": "M3", "panel_household": "PH9", "known_household": None,
         "region": "EU", "segment": "adult", "inclusion_probability": 0.5,
         "attended": True, "surveyed_on": "2026-08-25"},
        {"id": "S3", "match": "M4", "panel_household": "PH2", "known_household": "HH3",
         "region": "EU", "segment": "adult", "inclusion_probability": 0.5,
         "attended": True, "surveyed_on": "2026-08-25"},
    ]
    tables["recognized_delivery"] = [
        {"id": "R1", "origin_event": "E1", "contract": "C-A",
         "recognized_on": "2026-08-20", "credited_units": 1.0, "recognition_version": 1},
        {"id": "R2", "origin_event": "E2", "contract": "C-A",
         "recognized_on": "2026-08-20", "credited_units": 1.0, "recognition_version": 1},
    ]
    tables["invoices"] = [
        {"id": "I1", "contract": "C-A", "recognitions": ["R1", "R2"],
         "amount_minor": 2000, "issued_on": "2026-08-22", "paid_minor": 1000},
    ]
    tables["makegood_requests"] = [
        {"id": "Q1", "contract": "C-A", "ad_breaks": ["B3", "B4"],
         "requested_on": "2026-08-26", "accepted_breaks": ["B3"], "all_or_none": False},
    ]
    tables["later_evidence"] = [
        # 迟到家庭关联：D1 与 D2 在 6 月 1 日就是同一户，8 月 28 日才收到
        {"id": "L1", "kind": "household_confirm", "subject": "late-merge",
         "received_on": "2026-08-28",
         "payload": {"devices": ["D1", "D2"], "household": "HH1",
                     "effective_from": "2026-06-01"}},
    ]
    return tables


class World:
    def __init__(self, tables: dict[str, list[dict]] | None = None):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.tables = tables or standard_world()
        write_catalog(self.root, self.tables)
        self.ledger = Ledger(self.root)
        self.ledger.bootstrap()

    def close(self) -> None:
        self.ledger.close()
        self.directory.cleanup()

    def service(self, as_of: str | None = None) -> Service:
        return Service(self.ledger, as_of=as_of)


class HouseholdIdentityTests(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def tearDown(self):
        self.world.close()

    def test_late_link_merges_only_after_effective_date(self):
        graph = HouseholdGraph(self.world.tables["household_links"] + [{
            "id": "late", "devices": ["D1", "D2"], "household": "HH1",
            "status": "confirmed", "effective_from": "2026-06-01",
            "received_on": "2026-08-28",
        }])
        self.assertEqual(graph.resolve("D2", "2026-06-04"), ("household", "HH1"))
        # 证据到达前，商务不应假装已经知道这个迟到联系
        self.assertTrue(graph.known_on("D2", "2026-06-04", received_by="2026-08-27"))
        self.assertFalse(graph.known_on("D2", "2026-06-04", received_by="2026-08-27", only_link="late"))
        self.assertTrue(graph.known_on("D2", "2026-06-04", received_by="2026-08-28"))

    def test_possible_link_is_not_merged(self):
        graph = HouseholdGraph(self.world.tables["household_links"])
        self.assertEqual(graph.resolve("D4", "2026-06-02"), ("possible", "HH4"))
        self.assertEqual(graph.resolve("D5", "2026-06-02"), ("device", "D5"))


class PlaybackTests(unittest.TestCase):
    def test_duplicate_callbacks_yield_one_unit(self):
        result = build_play_units(standard_world()["playback"], [])
        units = [u for u in result.units if u.device == "D4"]
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].callbacks, 2)
        self.assertEqual(units[0].origin_events, ["E5", "E6"])
        # 资料支持范围：合格回传的最小与最大可见秒数
        self.assertEqual((units[0].visible_lower, units[0].visible_upper), (40, 50))

    def test_mirror_rows_are_collapsed(self):
        rows = [dict(standard_world()["playback"][0]) for _ in range(2)]
        rows[1]["id"] = "P1-mirror"
        result = build_play_units(rows, [])
        self.assertEqual(result.mirrored_rows, 1)
        self.assertEqual(len(result.units), 1)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def tearDown(self):
        self.world.close()

    def test_late_merge_deduplicates_households(self):
        before = self.world.service(as_of="2026-08-27").contract_report("C-A")
        after = self.world.service(as_of="2026-08-28").contract_report("C-A")
        # 迟到合并前 D1、D2 分属两个明确家庭；合并后只算一户
        self.assertEqual(before["delivery"]["lower"], 2)
        self.assertEqual(after["delivery"]["lower"], 1)
        # 迟到证据可以形成差额，但不抹掉已开票事实
        self.assertEqual(before["invoices"][0]["variance_minor"], 0)
        self.assertGreater(after["invoices"][0]["variance_minor"], 0)
        self.assertEqual(after["invoices"][0]["paid_minor"], 1000)

    def test_background_and_unlinked_are_explained_separately(self):
        report = self.world.service().contract_report("C-A")
        self.assertGreater(report["excluded_units"]["background"], 0)
        self.assertGreater(report["delivery"]["unlinked"], 0)
        reach = self.world.service().reach_explanation("C-A")
        self.assertGreaterEqual(reach["unlinked_device_units"], 1)
        self.assertIn("possible", json.dumps(reach, ensure_ascii=False))

    def test_competitor_cannot_claim_same_exclusive_break(self):
        report = self.world.service().contract_report("C-B")
        # 竞品在同一个排他段也有合格播放，归属未定，不能直接算已交付
        self.assertGreater(report["delivery"]["pending_units"], 0)
        self.assertEqual(report["delivery"]["lower"], 0)


class RecognitionTests(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def tearDown(self):
        self.world.close()

    def test_duplicate_household_recognition_is_not_supported(self):
        report = self.world.service().contract_report("C-A")
        items = report["recognitions"]["items"]
        reasons = {item["reason"] for item in items if not item["supported"]}
        self.assertIn("household_duplicate", reasons)
        self.assertEqual(report["recognitions"]["supported_units"], 1.0)
        self.assertEqual(report["recognitions"]["variance_minor"], 1000)


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def tearDown(self):
        self.world.close()

    def test_plan_respects_competitor_break_and_budget(self):
        service = self.world.service()
        plans = service.plans("conservative", "C-A")
        plan = plans[0]
        self.assertEqual(plan.selected[0]["break"], "B4" if plan.selected else "")
        breaks = [item["break"] for item in plan.selected]
        self.assertNotIn("B3", breaks)  # B3 已经由客户接受，不能再分配
        self.assertLessEqual(plan.spend_minor, plan.budget_minor)

    def test_accept_partial_makegood_creates_commitment(self):
        service = self.world.service()
        result = service.accept_makegood("Q1", ["B3"], "2026-08-30")
        self.assertEqual(result["ad_breaks"], ["B3"])
        break_report = service.ad_break_report("B3")
        self.assertEqual(break_report["accepted_contracts"], ["C-A"])

    def test_all_or_none_contract_cannot_split(self):
        service = self.world.service()
        # C-B 不可拆分；把它加入申请会失败（申请本身只属于 C-A）
        with self.assertRaises(KeyError):
            service.accept_makegood("Q-missing", ["B3"], "2026-08-30")


class MaterialTests(unittest.TestCase):
    def test_catalog_rejects_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tables = standard_world()
            write_catalog(root, tables)
            # 指纹不一致
            (root / "data" / "devices.jsonl").write_text("{}\n", encoding="utf-8")
            with self.assertRaises(MaterialError):
                seed_tables(root)

    def test_catalog_rejects_bad_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tables = standard_world()
            tables["playback"][0]["device"] = "D-missing"
            write_catalog(root, tables)
            with self.assertRaises(MaterialError):
                seed_tables(root)


if __name__ == "__main__":
    unittest.main()
