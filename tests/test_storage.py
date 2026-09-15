"""設定載入與 CSV 檔名比對。"""

import json
from unittest.mock import patch

from _util import TempDataCase

from core import storage


class TestLoadConfig(TempDataCase):
    def test_user_config_overrides_defaults(self):
        p = self.write_json("config.json", {"quota_gb": 5.0, "port": 9000})
        with patch.object(storage, "CONFIG_PATH", p):
            cfg = storage.load_config()
        self.assertEqual(cfg["quota_gb"], 5.0)
        self.assertEqual(cfg["port"], 9000)

    def test_unspecified_keys_keep_defaults(self):
        p = self.write_json("config.json", {"quota_gb": 5.0})
        with patch.object(storage, "CONFIG_PATH", p):
            cfg = storage.load_config()
        self.assertEqual(cfg["sample_interval_sec"],
                         storage.DEFAULT_CONFIG["sample_interval_sec"])
        self.assertEqual(cfg["gpss_script"], "gpss_api_query.py")

    def test_missing_file_is_created_with_defaults(self):
        p = self.tmp / "config.json"
        with patch.object(storage, "CONFIG_PATH", p):
            cfg = storage.load_config()
        self.assertTrue(p.exists())
        self.assertEqual(cfg["quota_gb"], storage.DEFAULT_CONFIG["quota_gb"])
        written = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(written["port"], storage.DEFAULT_CONFIG["port"])

    def test_corrupt_file_falls_back_to_defaults(self):
        p = self.tmp / "config.json"
        p.write_text("{ not json", encoding="utf-8")
        with patch.object(storage, "CONFIG_PATH", p):
            cfg = storage.load_config()
        self.assertEqual(cfg["quota_gb"], storage.DEFAULT_CONFIG["quota_gb"])

    def test_result_is_a_copy_not_the_defaults_dict(self):
        """load_config() 必須回傳副本，否則呼叫端改它會污染 DEFAULT_CONFIG。"""
        p = self.write_json("config.json", {})
        with patch.object(storage, "CONFIG_PATH", p):
            cfg = storage.load_config()
        cfg["quota_gb"] = 999
        self.assertNotEqual(storage.DEFAULT_CONFIG["quota_gb"], 999)


class TestDefaultConfigSafety(TempDataCase):
    def test_lan_ranges_never_swallow_gpss_subnet(self):
        """10.65.x.x 是對外（含 GPSS 檢索主機），絕不可被 lan_ranges 排除。
        預設值寫成 10.0.0.0/8 會讓檢索流量整個不計入額度。"""
        self.assertNotIn("10.0.0.0/8", storage.DEFAULT_CONFIG["lan_ranges"])
        self.assertIn("10.75.0.0/16", storage.DEFAULT_CONFIG["lan_ranges"])

    def test_count_ranges_pins_gpss_subnet(self):
        self.assertIn("10.65.0.0/16", storage.DEFAULT_CONFIG["count_ranges"])


class TestSummaryRe(TempDataCase):
    def test_new_style_with_machine(self):
        m = storage.SUMMARY_RE.search("summary_20260609_101530_PC17T033.csv")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "20260609")
        self.assertEqual(m.group(2), "PC17T033")

    def test_old_style_without_machine(self):
        m = storage.SUMMARY_RE.search("summary_20260609_101530.csv")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "20260609")
        self.assertIsNone(m.group(2))

    def test_non_matching(self):
        self.assertIsNone(storage.SUMMARY_RE.search("summary_2026_10.csv"))
        self.assertIsNone(storage.SUMMARY_RE.search("notes.csv"))


class TestGpssPath(TempDataCase):
    def test_joins_base_and_gpss_dir(self):
        with patch.object(storage, "BASE", self.tmp):
            p = storage.gpss_path({"gpss_dir": "program/gpss"}, "patents.txt")
        self.assertEqual(p, self.tmp / "program" / "gpss" / "patents.txt")
