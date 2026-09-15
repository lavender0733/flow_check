"""月進度計算的純函式測試。

基準日期：2026-09-14（週一）。2026 年 9 月共 30 天、22 個工作日；
截至 9/14 已過 10 個工作日，9/14（含）到月底還剩 13 個工作日。
"""

import datetime

from _util import TempDataCase

import monthly_progress as mp


class TestToInt(TempDataCase):
    def test_strips_commas_and_whitespace(self):
        self.assertEqual(mp.to_int("      6,552"), 6552)

    def test_plain_number(self):
        self.assertEqual(mp.to_int("42"), 42)

    def test_non_numeric_returns_zero(self):
        self.assertEqual(mp.to_int("N/A"), 0)
        self.assertEqual(mp.to_int(""), 0)
        self.assertEqual(mp.to_int(None), 0)

    def test_negative_is_not_a_digit_so_zero(self):
        # isdigit() 對 "-5" 回 False —— 這是現行行為，鎖定它
        self.assertEqual(mp.to_int("-5"), 0)


class TestColIndex(TempDataCase):
    def test_exact_match_wins(self):
        header = ["年月", "檢索次數", "檢索日期"]
        self.assertEqual(mp.col_index(header, "檢索次數"), 1)

    def test_falls_back_to_substring(self):
        header = ["年月", "本月檢索次數合計", "檢索日期"]
        self.assertEqual(mp.col_index(header, "檢索次數"), 1)

    def test_exact_match_preferred_over_earlier_substring(self):
        header = ["本月檢索次數合計", "檢索次數"]
        self.assertEqual(mp.col_index(header, "檢索次數"), 1)

    def test_missing_returns_none(self):
        self.assertIsNone(mp.col_index(["年月"], "檢索次數"))


class TestWorkdays(TempDataCase):
    def test_weekday_is_workday(self):
        self.assertTrue(mp.is_workday(datetime.date(2026, 9, 14), set(), set()))

    def test_weekend_is_not(self):
        self.assertFalse(mp.is_workday(datetime.date(2026, 9, 12), set(), set()))

    def test_holiday_is_not(self):
        self.assertFalse(
            mp.is_workday(datetime.date(2026, 9, 14), {"2026-09-14"}, set()))

    def test_makeup_weekend_is_workday(self):
        self.assertTrue(
            mp.is_workday(datetime.date(2026, 9, 12), set(), {"2026-09-12"}))

    def test_holiday_beats_makeup(self):
        # 同時列在假日與補班日時，假日勝出（現行行為）
        self.assertFalse(
            mp.is_workday(datetime.date(2026, 9, 12), {"2026-09-12"}, {"2026-09-12"}))

    def test_remaining_workdays_no_holidays(self):
        today = datetime.date(2026, 9, 14)
        self.assertEqual(mp.remaining_workdays(today, set(), set()), 13)

    def test_remaining_workdays_minus_one_holiday(self):
        today = datetime.date(2026, 9, 14)
        self.assertEqual(
            mp.remaining_workdays(today, {"2026-09-15"}, set()), 12)

    def test_month_workday_progress(self):
        total, elapsed = mp.month_workday_progress(
            datetime.date(2026, 9, 14), set(), set())
        self.assertEqual(total, 22)
        self.assertEqual(elapsed, 10)


class TestBasisDay(TempDataCase):
    def test_uses_data_date_when_in_month(self):
        got = mp.basis_day("20260910", "202609", datetime.date(2026, 9, 14))
        self.assertEqual(got, datetime.date(2026, 9, 10))

    def test_strips_separators(self):
        got = mp.basis_day("2026/09/10", "202609", datetime.date(2026, 9, 14))
        self.assertEqual(got, datetime.date(2026, 9, 10))

    def test_falls_back_when_other_month(self):
        today = datetime.date(2026, 9, 14)
        self.assertEqual(mp.basis_day("20260810", "202609", today), today)

    def test_falls_back_when_empty(self):
        today = datetime.date(2026, 9, 14)
        self.assertEqual(mp.basis_day("", "202609", today), today)
        self.assertEqual(mp.basis_day(None, "202609", today), today)

    def test_falls_back_when_invalid_date(self):
        today = datetime.date(2026, 9, 14)
        self.assertEqual(mp.basis_day("20260931", "202609", today), today)


class TestMaskCode(TempDataCase):
    def test_masks_after_four_chars(self):
        self.assertEqual(mp.mask_code("21dcBD8B96d02A77"),
                         "21dc" + "*" * 12)

    def test_short_code_unchanged(self):
        self.assertEqual(mp.mask_code("abcd"), "abcd")
        self.assertEqual(mp.mask_code("ab"), "ab")

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(mp.mask_code("  abcdef  "), "abcd**")

    def test_empty(self):
        self.assertEqual(mp.mask_code(""), "")
        self.assertEqual(mp.mask_code(None), "")


from unittest.mock import patch


def _snap(month, data_date, captured_at, total, machine="PC-A"):
    """組一筆快照。欄位取 record_snapshot() 實際寫出的子集。"""
    return {
        "captured_at": captured_at,
        "machine": machine,
        "month": month,
        "total": total,
        "simple_total": total // 2,
        "api_total": total - total // 2,
        "monthly_target": 450000,
        "percent": 0.0,
        "projected": 0,
        "on_track": False,
        "per_workday": 0,
        "data_date": data_date,
        "workdays_elapsed": 10,
        "workdays_total": 22,
        "target_to_date": 204545,
    }


class TestHistoryMerge(TempDataCase):
    """v7.2.1 回歸測試：快照依機器分檔，但顯示時必須合併所有機器。

    分檔只是寫入端的防撞機制；快照內容取自共用的匯出檔、是全站總量，
    與哪台電腦執行過檢索無關。讀取端若只看本機那份，別台留下的月份
    會整個從下拉選單消失。
    """

    def setUp(self):
        super().setUp()
        self.machines = self.tmp / "machines"
        self.machines.mkdir(parents=True, exist_ok=True)

    def _write_history(self, machine, snaps):
        self.write_json(f"machines/monthly_progress_history_{machine}.json", snaps)

    def test_load_all_history_merges_every_machine(self):
        self._write_history("PC-A", [_snap("2026-09", "20260910", "2026-09-10 09:00:00", 100)])
        self._write_history("PC-B", [_snap("2026-08", "20260820", "2026-08-20 09:00:00", 200)])
        with patch.object(mp, "MACHINE_DATA_DIR", self.machines):
            items = mp._load_all_history()
        self.assertEqual(len(items), 2)
        self.assertEqual({x["month"] for x in items}, {"2026-09", "2026-08"})

    def test_trend_months_include_other_machines(self):
        """核心回歸：8 月只存在於另一台的檔案裡，月份清單仍須列出它。"""
        self._write_history("PC-A", [_snap("2026-09", "20260910", "2026-09-10 09:00:00", 100)])
        self._write_history("PC-B", [_snap("2026-08", "20260820", "2026-08-20 09:00:00", 200)])
        with patch.object(mp, "MACHINE_DATA_DIR", self.machines):
            result = mp.trend("2026-09")
        self.assertIn("2026-08", result["months"])
        self.assertIn("2026-09", result["months"])

    def test_trend_can_select_other_machines_month(self):
        """直接指定別台留下的月份，必須取得該月的資料點（不是空的）。"""
        self._write_history("PC-A", [_snap("2026-09", "20260910", "2026-09-10 09:00:00", 100)])
        self._write_history("PC-B", [_snap("2026-08", "20260820", "2026-08-20 09:00:00", 200)])
        with patch.object(mp, "MACHINE_DATA_DIR", self.machines):
            result = mp.trend("2026-08")
        self.assertEqual(result["month"], "2026-08")
        self.assertEqual(len(result["points"]), 1)
        self.assertEqual(result["points"][0]["total"], 200)

    def test_same_data_date_keeps_latest_captured_at(self):
        """兩台在同一資料日期各按過更新 -> 只留 captured_at 較晚的那筆。"""
        self._write_history("PC-A", [
            _snap("2026-09", "20260910", "2026-09-10 09:00:00", 100, machine="PC-A"),
        ])
        self._write_history("PC-B", [
            _snap("2026-09", "20260910", "2026-09-10 17:00:00", 175, machine="PC-B"),
        ])
        with patch.object(mp, "MACHINE_DATA_DIR", self.machines):
            result = mp.trend("2026-09")
        self.assertEqual(len(result["points"]), 1)
        self.assertEqual(result["points"][0]["total"], 175)
        self.assertEqual(result["points"][0]["machine"], "PC-B")

    def test_point_date_comes_from_data_date_not_captured_at(self):
        """X 軸是匯出檔的資料最後日期，不是按更新按鈕的時間。"""
        self._write_history("PC-A", [
            _snap("2026-09", "20260910", "2026-09-14 23:00:00", 100),
        ])
        with patch.object(mp, "MACHINE_DATA_DIR", self.machines):
            result = mp.trend("2026-09")
        self.assertEqual(result["points"][0]["date"], "2026-09-10")

    def test_missing_data_date_falls_back_to_captured_at(self):
        """舊快照沒有 data_date 時退回建立日，不能整筆消失。"""
        snap = _snap("2026-09", "", "2026-09-11 08:30:00", 100)
        del snap["data_date"]
        self._write_history("PC-A", [snap])
        with patch.object(mp, "MACHINE_DATA_DIR", self.machines):
            result = mp.trend("2026-09")
        self.assertEqual(len(result["points"]), 1)
        self.assertEqual(result["points"][0]["date"], "2026-09-11")

    def test_one_corrupt_file_does_not_blank_the_chart(self):
        """單一檔案壞掉只略過它，不讓一台的問題把整張圖清空。"""
        self._write_history("PC-A", [_snap("2026-09", "20260910", "2026-09-10 09:00:00", 100)])
        (self.machines / "monthly_progress_history_PC-B.json").write_text(
            "{ this is not json", encoding="utf-8")
        with patch.object(mp, "MACHINE_DATA_DIR", self.machines):
            result = mp.trend("2026-09")
        self.assertEqual(len(result["points"]), 1)
        self.assertEqual(result["points"][0]["total"], 100)

    def test_missing_file_returns_empty_list(self):
        self.assertEqual(mp._read_history_file(self.tmp / "nope.json"), [])

    def test_non_list_json_returns_empty_list(self):
        p = self.write_json("machines/monthly_progress_history_PC-C.json", {"not": "a list"})
        self.assertEqual(mp._read_history_file(p), [])
