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
