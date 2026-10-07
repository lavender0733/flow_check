import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "program"))

from core.paths import XFER690_CONFIG_PATH
from xfer690.service import DEFAULT_XFER_CONFIG, load_xfer_config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "xfer690.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_returns_defaults(self):
        self.assertEqual(load_xfer_config(self.path), DEFAULT_XFER_CONFIG)

    def test_broken_json_returns_defaults(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(load_xfer_config(self.path), DEFAULT_XFER_CONFIG)

    def test_wiki_merged_one_level(self):
        self.path.write_text(json.dumps({
            "poll_interval_min": 5,
            "systems": [{"id": "x"}],
            "wiki": {"dry_run": False, "tables": [{"page": "p"}]},
        }), encoding="utf-8")
        cfg = load_xfer_config(self.path)
        self.assertEqual(cfg["poll_interval_min"], 5)
        self.assertEqual(cfg["systems"], [{"id": "x"}])
        self.assertFalse(cfg["wiki"]["dry_run"])
        self.assertEqual(cfg["wiki"]["tables"], [{"page": "p"}])
        # 沒給的 KM 欄位保留預設值
        self.assertEqual(cfg["wiki"]["locale"], "zh-tw")
        self.assertEqual(cfg["max_raw_kb"], 512)
        # 預設值本身沒被改到
        self.assertEqual(DEFAULT_XFER_CONFIG["wiki"]["tables"], [])

    def test_real_config_is_consistent(self):
        cfg = load_xfer_config(XFER690_CONFIG_PATH)
        systems = {s["id"]: s for s in cfg["systems"]}
        self.assertTrue(systems)
        owner = {}
        for s in cfg["systems"]:
            for code in s["auto"] + s["steps"]:
                self.assertNotIn(code, owner, f"{code} 同時屬於 {owner.get(code)} 和 {s['id']}")
                owner[code] = s["id"]
        daily_cols = {}
        for t in cfg["wiki"]["tables"]:
            self.assertIn(t["system"], systems)
            steps = systems[t["system"]]["steps"]
            for col, c in t["cols"].items():
                self.assertEqual(t["labels"][int(col)], c["label"], f"{t['system']} 欄 {col} 欄名")
                for k in ("step", "check"):
                    if k in c:
                        self.assertIn(c[k], steps, f"{t['system']} 欄 {col} 的 {k}")
                if t["page"] == "每日執行項目":
                    self.assertNotIn(col, daily_cols, f"每日表欄 {col} 重複")
                    daily_cols[col] = t["system"]
        self.assertEqual(len(cfg["wiki"]["tables"]), 12)
        self.assertEqual(cfg["wiki"]["nodata_value"], "-")


if __name__ == "__main__":
    unittest.main()
