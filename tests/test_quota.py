"""API 額度歸戶與時段計算。

歸戶算錯會讓額度數字錯，而畫面上看不出來 —— 這正是需要測試的那種錯。
"""

from datetime import datetime
from unittest.mock import patch

from _util import TempDataCase

import quota


def _at(y, m, d, hh, mm=0):
    """回傳一個 datetime 子類別，其 now() 固定在指定時刻。

    quota.py 是 `from datetime import datetime`，所以要換掉的是 quota 模組
    命名空間裡的那個名字。用子類別而非 Mock，replace()/strftime() 才照常可用。
    """
    fixed = datetime(y, m, d, hh, mm, 0)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    return _DT


CFG = {
    "busy_start_hour": 8,
    "busy_end_hour": 18,
    "dl_cap_busy": 10000,
    "dl_cap_offpeak": 30000,
    "case_cap_busy": 300,
    "case_cap_offpeak": 1000,
    "gpss_dir": "program/gpss",
}


class TestCurrentSegment(TempDataCase):
    def test_weekday_business_hours_is_busy(self):
        # 2026-09-14 是週一，10:00 在 08:00-18:00 之間
        with patch.object(quota, "datetime", _at(2026, 9, 14, 10)):
            busy, seg_start, dl_cap, case_cap = quota._current_segment(CFG)
        self.assertTrue(busy)
        self.assertEqual(seg_start, "2026-09-14 08:00:00")
        self.assertEqual(dl_cap, 10000)
        self.assertEqual(case_cap, 300)

    def test_weekday_evening_is_offpeak_same_day(self):
        with patch.object(quota, "datetime", _at(2026, 9, 14, 20)):
            busy, seg_start, dl_cap, case_cap = quota._current_segment(CFG)
        self.assertFalse(busy)
        self.assertEqual(seg_start, "2026-09-14 18:00:00")
        self.assertEqual(dl_cap, 30000)
        self.assertEqual(case_cap, 1000)

    def test_weekday_early_morning_is_offpeak_previous_day(self):
        # 07:00 還沒到 08:00 -> 下班時段，起點是「昨天 18:00」
        with patch.object(quota, "datetime", _at(2026, 9, 14, 7)):
            busy, seg_start, _, _ = quota._current_segment(CFG)
        self.assertFalse(busy)
        self.assertEqual(seg_start, "2026-09-13 18:00:00")

    def test_weekend_is_always_offpeak(self):
        # 2026-09-12 是週六，即使 10:00 也算下班時段
        with patch.object(quota, "datetime", _at(2026, 9, 12, 10)):
            busy, seg_start, dl_cap, _ = quota._current_segment(CFG)
        self.assertFalse(busy)
        self.assertEqual(seg_start, "2026-09-11 18:00:00")
        self.assertEqual(dl_cap, 30000)

    def test_boundary_eighteen_is_offpeak(self):
        # 18:00 整已屬下班時段（bs <= hour < be 不含 18）
        with patch.object(quota, "datetime", _at(2026, 9, 14, 18)):
            busy, seg_start, _, _ = quota._current_segment(CFG)
        self.assertFalse(busy)
        self.assertEqual(seg_start, "2026-09-14 18:00:00")

    def test_boundary_eight_is_busy(self):
        with patch.object(quota, "datetime", _at(2026, 9, 14, 8)):
            busy, _, _, _ = quota._current_segment(CFG)
        self.assertTrue(busy)


class TestEffectiveCode(TempDataCase):
    def test_recorded_code_wins(self):
        """output 有記碼（新資料）就用那個碼 —— 切換驗證碼後分帳才正確。"""
        code_of = {"PC-A": "machine-default"}
        self.assertEqual(
            quota._effective_code("recorded-code", "PC-A", code_of), "recorded-code")

    def test_web_method_returns_none(self):
        """傳統檢索不耗 API 額度，回 None 讓呼叫端略過。"""
        self.assertIsNone(quota._effective_code("web", "PC-A", {}))

    def test_web_is_case_insensitive(self):
        self.assertIsNone(quota._effective_code("WEB", "PC-A", {}))
        self.assertIsNone(quota._effective_code(" Web ", "PC-A", {}))

    def test_empty_falls_back_to_machine_code(self):
        """舊資料沒記碼 -> 退回機器目前選的碼。"""
        code_of = {"PC-A": "machine-default"}
        self.assertEqual(quota._effective_code("", "PC-A", code_of), "machine-default")
        self.assertEqual(quota._effective_code(None, "PC-A", code_of), "machine-default")

    def test_unknown_machine_falls_back_to_empty(self):
        self.assertEqual(quota._effective_code("", "PC-UNKNOWN", {}), "")

    def test_surrounding_whitespace_stripped(self):
        self.assertEqual(quota._effective_code("  abc  ", "PC-A", {}), "abc")


class TestCodeOfMachine(TempDataCase):
    def _gpss_dir(self):
        d = self.tmp / "program" / "gpss"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_reads_each_machine_config(self):
        self._gpss_dir()
        self.write_json("program/gpss/api_config_PC-A.json", {"userCode": "code-a"})
        self.write_json("program/gpss/api_config_PC-B.json", {"userCode": "code-b"})
        with patch.object(quota, "BASE", self.tmp):
            got = quota._code_of_machine({"gpss_dir": "program/gpss"})
        self.assertEqual(got, {"PC-A": "code-a", "PC-B": "code-b"})

    def test_skips_drive_conflict_copies(self):
        """Google Drive 衝突複本的檔名含空白與括號，MID_RE 擋掉。"""
        self._gpss_dir()
        self.write_json("program/gpss/api_config_PC-A.json", {"userCode": "code-a"})
        self.write_json("program/gpss/api_config_PC-A (1).json", {"userCode": "dup"})
        with patch.object(quota, "BASE", self.tmp):
            got = quota._code_of_machine({"gpss_dir": "program/gpss"})
        self.assertEqual(got, {"PC-A": "code-a"})

    def test_corrupt_file_yields_empty_code_not_crash(self):
        d = self._gpss_dir()
        (d / "api_config_PC-A.json").write_text("{ broken", encoding="utf-8")
        with patch.object(quota, "BASE", self.tmp):
            got = quota._code_of_machine({"gpss_dir": "program/gpss"})
        self.assertEqual(got, {"PC-A": ""})

    def test_shared_api_config_is_not_treated_as_a_machine(self):
        """api_config.json（無機器後綴）不該被當成某台機器。"""
        self._gpss_dir()
        self.write_json("program/gpss/api_config.json", {"patDB": "TW"})
        with patch.object(quota, "BASE", self.tmp):
            got = quota._code_of_machine({"gpss_dir": "program/gpss"})
        self.assertEqual(got, {})


class TestMaskCodeConsistency(TempDataCase):
    def test_matches_monthly_progress_format(self):
        """兩張卡要對齊，遮罩格式必須一致。"""
        import monthly_progress as mp
        for code in ("21dcBD8B96d02A77", "abcd", "ab", "", "  abcdef  "):
            self.assertEqual(quota._mask_code(code), mp.mask_code(code),
                             f"遮罩格式不一致：{code!r}")
