import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_complete_interference_workflow(self):
        item = self.service.create_item({
            "frequency_mhz": 2400.0,
            "bandwidth_mhz": 20.0,
            "station_id": "ST-01",
            "region": "north",
            "strength_dbm": -35,
            "detected_at": "2026-09-27T10:00:00+00:00",
            "reporter": "monitor-1",
        }, "analyst-1", "analyst")
        item = self.service.act(item["id"], "assess", {}, "analyst-1", "analyst", item["version"])
        self.assertEqual(item["payload"]["assessment"]["level"], "critical")
        item = self.service.act(item["id"], "locate", {"location": "cell-7", "confidence": 0.9}, "field-1", "field_operator", item["version"])
        # 停用前需至少三个不同监测站在同一时频窗内形成联合证据
        self.service.add_source(item["id"], {
            "source_type": "monitor_scan",
            "external_id": "scan-st02-1",
            "station_id": "ST-02",
            "region": "north",
            "frequency_mhz": 2400.02,
            "observed_at": "2026-09-27T10:05:00+00:00",
            "strength_dbm": -40,
        }, "monitor-2", "monitor", "north")
        self.service.add_source(item["id"], {
            "source_type": "monitor_scan",
            "external_id": "scan-st03-1",
            "station_id": "ST-03",
            "region": "north",
            "frequency_mhz": 2399.98,
            "observed_at": "2026-09-27T10:08:00+00:00",
            "strength_dbm": -32,
        }, "monitor-3", "monitor", "north")
        item = self.service.get_item(item["id"])
        self.assertTrue(item["correlation"]["consensus"]["reached"])
        self.assertEqual(item["correlation"]["groups"][0]["earliest_observed_at"], "2026-09-27T10:00:00+00:00")
        self.assertEqual(item["correlation"]["groups"][0]["strongest_signal_dbm"], -32.0)
        item = self.service.act(item["id"], "suspend", {"authorization_code": "REG-NORTH-1"}, "coord-1", "coordinator", item["version"], "north")
        item = self.service.act(item["id"], "coordinate", {"coordination_agreement": "AGC-7"}, "coord-1", "coordinator", item["version"], "north")
        item = self.service.act(item["id"], "resolve", {"measurement_cleared": True, "evidence": "scan-7"}, "coord-1", "coordinator", item["version"], "north")
        self.assertEqual(item["status"], "resolved")
        self.assertGreaterEqual(len(item["audit"]), 6)


if __name__ == "__main__":
    unittest.main()
