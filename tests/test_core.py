import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import npu_monitor as nm


NPU_SAMPLE = r"""
+-------------------------------------------------------------+
| 0     Ascend910           | OK            | -            49        0    / 0                |
| 0     0                   | 0000:AE:00.0  | 0            0    / 0   58815/ 65536          |
+-------------------------------------------------------------+
| 0     Ascend910           | OK            | 179.5        42        0    / 0                |
| 1     1                   | 0000:95:00.0  | 12.5         0    / 0   2862 / 65536          |
+-------------------------------------------------------------+
"""


class ParserTests(unittest.TestCase):
    def test_power_missing_does_not_shift_temperature(self):
        chips = nm.parse_npu_smi(NPU_SAMPLE)
        self.assertEqual(2, len(chips))
        self.assertIsNone(chips[0]["power"])
        self.assertEqual(49.0, chips[0]["temp"])
        self.assertEqual(179.5, chips[1]["power"])
        self.assertEqual(42.0, chips[1]["temp"])

    def test_process_table_and_container_mapping(self):
        text = (
            "| 0       0                 | 1104570       | VLLMWorker_PP      | 55637                 | NA                      |\n"
            "| 1       0                 | 2200001       | VLLMWorker_PP      | 12000                 |\n"
        )
        procs = nm.parse_npu_processes(text)
        self.assertEqual(2, len(procs))
        self.assertEqual((0, 1104570), (procs[0]["npu"], procs[0]["pid"]))
        self.assertEqual((1, 2200001), (procs[1]["npu"], procs[1]["pid"]))


class ContainerDisplayTests(unittest.TestCase):
    def test_multiple_containers_on_different_npus_are_both_shown(self):
        chips = [
            {"npu": 0, "grp": 0, "chip": 0, "_busy": True, "containers": []},
            {"npu": 1, "grp": 0, "chip": 1, "_busy": True, "containers": []},
        ]
        procs = [
            {"npu": 0, "pid": 100},
            {"npu": 1, "pid": 200},
        ]
        names = nm.attach_container_info(
            chips, procs, "100\tcontainer-a\n200\tcontainer-b\n")
        self.assertEqual(["container-a", "container-b"], names)
        self.assertEqual(["container-a"], chips[0]["containers"])
        self.assertEqual(["container-b"], chips[1]["containers"])

        snap = {"ok": True, "busy": True, "chips": chips}
        self.assertEqual(["container-a", "container-b"],
                         nm.busy_containers(snap))
        self.assertEqual("占用容器: container-a / container-b",
                         nm.container_line(snap))

    def test_same_container_on_two_npus_is_deduplicated(self):
        chips = [
            {"npu": 0, "_busy": True, "containers": []},
            {"npu": 1, "_busy": True, "containers": []},
        ]
        procs = [{"npu": 0, "pid": 100}, {"npu": 1, "pid": 200}]
        nm.attach_container_info(
            chips, procs, "100\tshared-container\n200\tshared-container\n")
        snap = {"ok": True, "busy": True, "chips": chips}
        self.assertEqual(["shared-container"], nm.busy_containers(snap))

    def test_idle_chip_container_is_not_shown(self):
        chips = [
            {"npu": 0, "_busy": True, "containers": ["busy-container"]},
            {"npu": 1, "_busy": False, "containers": ["idle-container"]},
        ]
        snap = {"ok": True, "busy": True, "chips": chips}
        self.assertEqual(["busy-container"], nm.busy_containers(snap))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.old_path = nm.CFG_PATH
        self.tmp = tempfile.TemporaryDirectory(dir=HERE)
        self.path = os.path.join(self.tmp.name, "npu_monitor.json")
        nm.CFG_PATH = self.path

    def tearDown(self):
        nm.CFG_PATH = self.old_path
        self.tmp.cleanup()

    def test_invalid_json_falls_back_and_backs_up(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        cfg = nm.load_cfg()
        self.assertEqual(60, cfg["interval"])
        self.assertEqual(3, cfg["defaults"]["idle_cycles"])
        self.assertTrue(nm.CONFIG_WARNINGS)
        backups = [n for n in os.listdir(self.tmp.name) if ".bad-" in n]
        self.assertEqual(1, len(backups))

    def test_servers_are_normalized_and_empty_user_is_preserved(self):
        payload = {
            "interval": 5,
            "defaults": {"idle_core": 0, "idle_hbm_gb": 8, "idle_cycles": 3},
            "servers": [
                None,
                {"id": "x", "host": "host-a", "user": "", "port": "bad",
                 "idle_core": 0},
                {"id": "x", "host": "host-b", "port": 65536},
            ],
        }
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        cfg = nm.load_cfg()
        self.assertEqual(5, cfg["interval"])       # valid explicit interval is kept
        self.assertEqual(2, len(cfg["servers"]))
        first = cfg["servers"][0]
        self.assertEqual("host-a", first["host"])
        self.assertEqual("", first["user"])
        self.assertEqual(22, first["port"])
        self.assertEqual(0, first["idle_core"])
        self.assertNotEqual(cfg["servers"][0]["id"], cfg["servers"][1]["id"])
        self.assertEqual(65535, cfg["servers"][1]["port"])


if __name__ == "__main__":
    unittest.main()
