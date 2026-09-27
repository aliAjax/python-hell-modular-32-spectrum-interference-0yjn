import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.correlation import correlate, consensus_gap
from src.domain import DomainError
from src.repository import Repository
from src.service import Service


def make_source(source_id, station_id, observed_at, strength=-50.0,
                frequency_mhz=2400.0, region="north", external_id=None):
    return {
        "id": source_id,
        "source_type": "monitor_scan",
        "external_id": external_id or ("ext-%s" % source_id),
        "observed_at": observed_at,
        "payload": {
            "station_id": station_id,
            "region": region,
            "frequency_mhz": frequency_mhz,
            "strength_dbm": strength,
        },
    }


ITEM = {
    "station_id": "ST-01",
    "region": "north",
    "frequency_mhz": 2400.0,
    "detected_at": "2026-09-27T10:00:00+00:00",
    "strength_dbm": -45.0,
    "bandwidth_mhz": 20.0,
}


class CorrelationRuleTest(unittest.TestCase):
    def test_same_region_frequency_and_time_window_form_one_group(self):
        sources = [
            make_source(1, "ST-02", "2026-09-27T10:05:00+00:00", -40.0, 2400.05),
            make_source(2, "ST-03", "2026-09-27T10:10:00+00:00", -33.0, 2400.00),
        ]
        digest = correlate(ITEM, sources)
        self.assertEqual(len(digest["groups"]), 1)
        group = digest["groups"][0]
        self.assertEqual(group["stations"], ["ST-01", "ST-02", "ST-03"])
        self.assertEqual(group["earliest_observed_at"], "2026-09-27T10:00:00+00:00")
        self.assertEqual(group["latest_observed_at"], "2026-09-27T10:10:00+00:00")
        self.assertEqual(group["strongest_signal_dbm"], -33.0)
        self.assertTrue(group["includes_primary"])
        self.assertTrue(group["consensus"])
        self.assertTrue(digest["consensus"]["reached"])

    def test_boundary_values_are_included(self):
        # 频点差恰好 0.05MHz、时间差恰好 10 分钟仍属于同一联合证据
        sources = [
            make_source(1, "ST-02", "2026-09-27T09:50:00+00:00", -50.0, 2399.95),
        ]
        digest = correlate(ITEM, sources)
        self.assertEqual(len(digest["groups"]), 1)

    def test_frequency_gap_splits_groups(self):
        sources = [
            make_source(1, "ST-02", "2026-09-27T10:02:00+00:00", -50.0, 2400.06),
        ]
        digest = correlate(ITEM, sources)
        self.assertEqual(len(digest["groups"]), 2)
        self.assertTrue(digest["groups"][0]["includes_primary"])

    def test_time_gap_splits_groups(self):
        sources = [
            make_source(1, "ST-02", "2026-09-27T10:11:00+00:00", -50.0, 2400.01),
        ]
        digest = correlate(ITEM, sources)
        self.assertEqual(len(digest["groups"]), 2)

    def test_different_region_never_correlates(self):
        sources = [
            make_source(1, "ST-02", "2026-09-27T10:02:00+00:00", -50.0, 2400.01, region="south"),
        ]
        digest = correlate(ITEM, sources)
        self.assertEqual(len(digest["groups"]), 2)

    def test_consensus_gap_names_missing_station(self):
        sources = [make_source(1, "ST-02", "2026-09-27T10:05:00+00:00")]
        digest = correlate(ITEM, sources)
        gap = consensus_gap(digest, expected_stations=["ST-01", "ST-02", "ST-03"])
        self.assertEqual(gap["present"], ["ST-01", "ST-02"])
        self.assertEqual(gap["missing_stations"], ["ST-03"])
        self.assertEqual(gap["shortage"], 1)


class CorrelationServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "frequency_mhz": 2400.0,
            "bandwidth_mhz": 20.0,
            "station_id": "ST-01",
            "region": "north",
            "strength_dbm": -45,
            "detected_at": "2026-09-27T10:00:00+00:00",
            "reporter": "monitor-1",
        }, "analyst-1", "analyst")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _add(self, station, observed_at, strength=-50.0, frequency_mhz=2400.0, ext=None):
        self.service.add_source(self.item["id"], {
            "source_type": "monitor_scan",
            "external_id": ext or ("ext-%s" % station),
            "station_id": station,
            "region": "north",
            "frequency_mhz": frequency_mhz,
            "observed_at": observed_at,
            "strength_dbm": strength,
        }, "m", "monitor", "north")

    def _reach_located(self):
        fresh = self.service.get_item(self.item["id"])
        item = self.service.act(self.item["id"], "assess", {}, "a", "analyst", fresh["version"])
        return self.service.act(
            item["id"], "locate", {"location": "cell-7", "confidence": 0.9},
            "f", "field_operator", item["version"],
        )

    def test_every_new_source_leaves_correlation_audit(self):
        self._add("ST-02", "2026-09-27T10:05:00+00:00")
        item = self.service.get_item(self.item["id"])
        types = [event["event_type"] for event in item["audit"]]
        self.assertIn("source_recorded", types)
        self.assertIn("correlation_updated", types)
        updated = [e for e in item["audit"] if e["event_type"] == "correlation_updated"][0]
        self.assertEqual(updated["payload"]["stations_before"], ["ST-01"])
        self.assertEqual(updated["payload"]["stations_after"], ["ST-01", "ST-02"])
        self.assertEqual(updated["payload"]["assigned_group"], "G1")
        self.assertFalse(updated["payload"]["consensus_after"])

    def test_suspend_blocked_until_three_stations_and_names_missing(self):
        item = self._reach_located()
        with self.assertRaises(DomainError) as gap:
            self.service.act(item["id"], "suspend", {
                "authorization_code": "REG-NORTH-9",
                "expected_stations": ["ST-01", "ST-02", "ST-03"],
            }, "c", "coordinator", item["version"], "north")
        self.assertEqual(gap.exception.code, "insufficient_consensus")
        self.assertIn("ST-03", str(gap.exception))

        self._add("ST-02", "2026-09-27T10:05:00+00:00")
        with self.assertRaises(DomainError) as still_short:
            self.service.act(item["id"], "suspend", {
                "authorization_code": "REG-NORTH-9",
            }, "c", "coordinator", item["version"], "north")
        self.assertEqual(still_short.exception.code, "insufficient_consensus")
        self.assertIn("仍缺 1 个不同监测站", str(still_short.exception))

        self._add("ST-03", "2026-09-27T10:08:00+00:00", strength=-31.0, frequency_mhz=2399.97)
        fresh = self.service.get_item(item["id"])
        suspended = self.service.act(item["id"], "suspend", {
            "authorization_code": "REG-NORTH-9",
        }, "c", "coordinator", fresh["version"], "north")
        self.assertEqual(suspended["status"], "suspended")
        self.assertEqual(suspended["payload"]["suspend_authorization"], "REG-NORTH-9")

    def test_unrelated_source_does_not_count_as_consensus(self):
        item = self._reach_located()
        # 同一站重复上报不算新站
        self._add("ST-01", "2026-09-27T10:04:00+00:00", ext="ext-ST-01-2")
        # 时频窗外的第三站不算共识
        self._add("ST-99", "2026-09-27T11:30:00+00:00")
        with self.assertRaises(DomainError) as gap:
            self.service.act(item["id"], "suspend", {
                "authorization_code": "REG-NORTH-9",
            }, "c", "coordinator", item["version"], "north")
        self.assertEqual(gap.exception.code, "insufficient_consensus")

    def test_issued_authorization_survives_later_correlation(self):
        self._add("ST-02", "2026-09-27T10:05:00+00:00")
        self._add("ST-03", "2026-09-27T10:08:00+00:00")
        item = self._reach_located()
        suspended = self.service.act(item["id"], "suspend", {
            "authorization_code": "REG-NORTH-9",
        }, "c", "coordinator", item["version"], "north")
        self.assertEqual(suspended["payload"]["suspend_authorization"], "REG-NORTH-9")

        # 授权签发后继续归并新来源，关联摘要更新但授权编号不变
        self._add("ST-04", "2026-09-27T10:09:00+00:00", ext="ext-ST-04")
        refreshed = self.service.get_item(item["id"])
        self.assertEqual(refreshed["payload"]["suspend_authorization"], "REG-NORTH-9")
        self.assertEqual(refreshed["correlation"]["groups"][0]["stations"],
                         ["ST-01", "ST-02", "ST-03", "ST-04"])

        # 同一编号可幂等重放，换编号则被拒绝
        self.service.act(item["id"], "suspend", {
            "authorization_code": "REG-NORTH-9",
        }, "c", "coordinator", refreshed["version"], "north")
        with self.assertRaises(DomainError) as immutable:
            self.service.act(item["id"], "suspend", {
                "authorization_code": "REG-NORTH-10",
            }, "c", "coordinator", self.service.get_item(item["id"])["version"], "north")
        self.assertEqual(immutable.exception.code, "authorization_immutable")
        self.assertEqual(
            self.service.get_item(item["id"])["payload"]["suspend_authorization"],
            "REG-NORTH-9",
        )


if __name__ == "__main__":
    unittest.main()
