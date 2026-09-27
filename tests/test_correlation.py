import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src import correlation
from src.audit import audit_hash
from src.repository import Repository
from src.service import Service
from src.domain import DomainError


def observation(ref, station, region="north", frequency=100.0, observed_at="2026-09-27T10:00:00+00:00", strength=-50.0):
    return {
        "ref": ref,
        "station_id": station,
        "region": region,
        "frequency_mhz": frequency,
        "observed_at": observed_at,
        "strength_dbm": strength,
    }


class CorrelationRuleTest(unittest.TestCase):
    def clusters(self, observations, expected=None):
        return correlation.summarize(observations, expected)["clusters"]

    def test_frequency_boundary(self):
        at_boundary = self.clusters([
            observation("a", "ST-01", frequency=100.00),
            observation("b", "ST-02", frequency=100.05),
        ])
        self.assertEqual(len(at_boundary), 1)
        over_boundary = self.clusters([
            observation("a", "ST-01", frequency=100.00),
            observation("b", "ST-02", frequency=100.06),
        ])
        self.assertEqual(len(over_boundary), 2)

    def test_time_boundary(self):
        within = self.clusters([
            observation("a", "ST-01", observed_at="2026-09-27T10:00:00+00:00"),
            observation("b", "ST-02", observed_at="2026-09-27T10:10:00+00:00"),
        ])
        self.assertEqual(len(within), 1)
        outside = self.clusters([
            observation("a", "ST-01", observed_at="2026-09-27T10:00:00+00:00"),
            observation("b", "ST-02", observed_at="2026-09-27T10:11:00+00:00"),
        ])
        self.assertEqual(len(outside), 2)

    def test_region_isolation(self):
        clusters = self.clusters([
            observation("a", "ST-01", region="north"),
            observation("b", "ST-02", region="south"),
        ])
        self.assertEqual(len(clusters), 2)

    def test_chain_association_is_transitive(self):
        # A-B 满足阈值、B-C 满足阈值，但 A-C 频点相差 0.10MHz；链式传递后仍属同一联合证据
        clusters = self.clusters([
            observation("a", "ST-01", frequency=100.00, observed_at="2026-09-27T10:00:00+00:00"),
            observation("b", "ST-02", frequency=100.05, observed_at="2026-09-27T10:05:00+00:00"),
            observation("c", "ST-03", frequency=100.10, observed_at="2026-09-27T10:10:00+00:00"),
        ])
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["station_ids"], ["ST-01", "ST-02", "ST-03"])
        self.assertTrue(clusters[0]["consensus_reached"])

    def test_summary_earliest_strongest_and_stations(self):
        clusters = self.clusters([
            observation("item", "ST-01", frequency=100.0, observed_at="2026-09-27T10:00:00+00:00", strength=-50),
            observation("s1", "ST-02", frequency=100.02, observed_at="2026-09-27T09:55:00+00:00", strength=-30),
            observation("s2", "ST-01", frequency=100.03, observed_at="2026-09-27T10:04:00+00:00", strength=-60),
        ])
        cluster = clusters[0]
        self.assertEqual(cluster["first_observed_at"], "2026-09-27T09:55:00+00:00")
        self.assertEqual(cluster["strongest"]["station_id"], "ST-02")
        self.assertEqual(cluster["strongest"]["strength_dbm"], -30.0)
        self.assertEqual(cluster["station_ids"], ["ST-02", "ST-01"])
        self.assertEqual(cluster["station_count"], 2)

    def test_missing_station_listed(self):
        clusters = self.clusters(
            [observation("item", "ST-01"), observation("s1", "ST-02")],
            expected=["ST-01", "ST-02", "ST-03"],
        )
        self.assertFalse(clusters[0]["consensus_reached"])
        self.assertEqual(clusters[0]["missing_station_count"], 1)
        self.assertEqual(clusters[0]["missing_stations"], ["ST-03"])


class CorrelationWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _create(self, expected_stations=None):
        payload = {
            "frequency_mhz": 100.0,
            "bandwidth_mhz": 1.0,
            "station_id": "ST-01",
            "region": "north",
            "strength_dbm": -50,
            "detected_at": "2026-09-27T10:00:00+00:00",
            "reporter": "monitor-1",
        }
        if expected_stations is not None:
            payload["expected_stations"] = expected_stations
        return self.service.create_item(payload, "m", "monitor")

    def _source(self, item_id, external_id, station, observed_at, strength=-45, frequency=None):
        payload = {
            "source_type": "rf_sensor",
            "external_id": external_id,
            "observed_at": observed_at,
            "strength_dbm": strength,
            "station_id": station,
        }
        if frequency is not None:
            payload["frequency_mhz"] = frequency
        return self.service.add_source(item_id, payload, "m", "monitor")

    def _to_located(self, item):
        item = self.service.act(item["id"], "assess", {}, "m", "monitor", item["version"])
        return self.service.act(item["id"], "locate", {"location": "cell-1", "confidence": 0.8}, "f", "field_operator", item["version"])

    def test_every_new_source_leaves_audit_change(self):
        item = self._create()
        self._source(item["id"], "E-2", "ST-02", "2026-09-27T10:05:00+00:00")
        # 频点相差 0.10MHz 的来源先各自成簇
        self._source(item["id"], "E-3", "ST-03", "2026-09-27T10:00:00+00:00", frequency=100.10)
        # 桥接来源同时满足与两簇的阈值，触发归并
        self._source(item["id"], "E-4", "ST-04", "2026-09-27T10:05:00+00:00", frequency=100.05)
        item = self.service.get_item(item["id"])
        changes = [event["payload"]["change"] for event in item["audit"] if event["event_type"] == "correlation_updated"]
        self.assertEqual(changes, ["joined", "formed", "merged"])
        merged = item["audit"][-1]["payload"]
        self.assertEqual(merged["merged_cluster_ids"], ["item", "src-2"])
        self.assertTrue(merged["consensus_reached"])
        self.assertEqual(len(item["correlation"]["clusters"]), 1)
        self.assertEqual(item["correlation"]["clusters"][0]["station_ids"], ["ST-01", "ST-03", "ST-02", "ST-04"])

    def test_audit_hash_chain_intact_after_correlation(self):
        item = self._create()
        self._source(item["id"], "E-2", "ST-02", "2026-09-27T10:03:00+00:00")
        item = self.service.get_item(item["id"])
        previous = "GENESIS"
        for event in item["audit"]:
            body = {
                "item_id": event["item_id"],
                "event_type": event["event_type"],
                "actor": event["actor"],
                "role": event["role"],
                "payload": event["payload"],
                "created_at": event["created_at"],
            }
            self.assertEqual(event["previous_hash"], previous)
            previous = audit_hash(previous, body)
            self.assertEqual(event["event_hash"], previous)

    def test_suspend_blocked_until_three_stations_and_names_missing_station(self):
        item = self._create(expected_stations=["ST-01", "ST-02", "ST-03"])
        self._source(item["id"], "E-2", "ST-02", "2026-09-27T10:04:00+00:00")
        item = self._to_located(self.service.get_item(item["id"]))
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "suspend", {"authorization_code": "REG-N-1"}, "c", "coordinator", item["version"], "north")
        self.assertEqual(context.exception.code, "insufficient_consensus")
        self.assertEqual(context.exception.status, 409)
        self.assertIn("ST-03", str(context.exception))
        self.assertIn("仍缺", str(context.exception))

    def test_suspend_message_without_expected_stations_gives_count(self):
        item = self._create()
        item = self._to_located(item)
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "suspend", {"authorization_code": "REG-N-1"}, "c", "coordinator", item["version"], "north")
        self.assertIn("仍缺 2 个监测站", str(context.exception))

    def test_third_station_unlocks_suspend_and_freezes_evidence(self):
        item = self._create()
        self._source(item["id"], "E-2", "ST-02", "2026-09-27T10:03:00+00:00", strength=-30)
        self._source(item["id"], "E-3", "ST-03", "2026-09-27T10:08:00+00:00", strength=-33)
        item = self._to_located(self.service.get_item(item["id"]))
        item = self.service.act(item["id"], "suspend", {"authorization_code": "REG-N-9"}, "c", "coordinator", item["version"], "north")
        self.assertEqual(item["status"], "suspended")
        self.assertEqual(item["payload"]["suspend_authorization"], "REG-N-9")
        evidence = item["payload"]["consensus_evidence"]
        self.assertEqual(evidence["station_count"], 3)
        self.assertEqual(evidence["station_ids"], ["ST-01", "ST-02", "ST-03"])
        self.assertEqual(evidence["strongest"]["station_id"], "ST-02")

        # 已签发授权之后继续归并新来源：授权与共识快照不得被改写
        self._source(item["id"], "E-4", "ST-04", "2026-09-27T10:09:00+00:00", strength=-20)
        refreshed = self.service.get_item(item["id"])
        self.assertEqual(refreshed["status"], "suspended")
        self.assertEqual(refreshed["payload"]["suspend_authorization"], "REG-N-9")
        self.assertEqual(refreshed["payload"]["consensus_evidence"]["station_count"], 3)
        # 派生视图反映新的关联事实
        self.assertEqual(refreshed["correlation"]["clusters"][0]["station_count"], 4)
        self.assertEqual(refreshed["correlation"]["clusters"][0]["strongest"]["station_id"], "ST-04")
        last_event = refreshed["audit"][-1]
        self.assertEqual(last_event["event_type"], "correlation_updated")
        self.assertEqual(last_event["payload"]["station_ids"], ["ST-01", "ST-02", "ST-03", "ST-04"])

    def test_source_without_frequency_inherits_item_frequency(self):
        item = self._create()
        self._source(item["id"], "E-2", "ST-02", "2026-09-27T10:02:00+00:00")
        self._source(item["id"], "E-3", "ST-03", "2026-09-27T10:04:00+00:00")
        summary = self.service.get_item(item["id"])["correlation"]
        self.assertEqual(len(summary["clusters"]), 1)
        self.assertTrue(summary["consensus_reached"])

    def test_unknown_station_source_does_not_count_to_consensus(self):
        item = self._create()
        self._source(item["id"], "E-2", None, "2026-09-27T10:02:00+00:00")
        self._source(item["id"], "E-3", None, "2026-09-27T10:04:00+00:00")
        cluster = self.service.get_item(item["id"])["correlation"]["clusters"][0]
        self.assertEqual(cluster["member_count"], 3)
        self.assertEqual(cluster["station_count"], 1)
        self.assertFalse(cluster["consensus_reached"])


if __name__ == "__main__":
    unittest.main()
