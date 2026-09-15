"""彙整 Excel 的檔名與欄位解析。

這是統計的入口：檔名解析錯會把 Google Drive 衝突複本當成「多一台機器」
重複計數；欄位對應錯則整條統計線都錯。
"""

from _util import TempDataCase

from gpss import output_store as os_


class TestDateHelpers(TempDataCase):
    def test_sheet_name(self):
        self.assertEqual(os_._sheet_for("20260609"), "2026-06-09")

    def test_month_key(self):
        self.assertEqual(os_._month_of_d8("20260609"), "2026-06")


class TestParseXlsxName(TempDataCase):
    def test_new_style_with_month(self):
        self.assertEqual(os_._parse_xlsx_name("summary_PC17T033_2026-06.xlsx"),
                         ("PC17T033", "2026-06"))

    def test_old_style_without_month(self):
        self.assertEqual(os_._parse_xlsx_name("summary_PC17T033.xlsx"),
                         ("PC17T033", None))

    def test_machine_id_with_hyphen(self):
        self.assertEqual(os_._parse_xlsx_name("summary_DESKTOP-V33GG01_2026-08.xlsx"),
                         ("DESKTOP-V33GG01", "2026-08"))

    def test_drive_conflict_copy_rejected(self):
        """summary_PC17T033_2026-06 (1).xlsx 這種衝突複本必須回 None，
        否則會被當成另一台機器、造成重複計數。"""
        self.assertIsNone(os_._parse_xlsx_name("summary_PC17T033_2026-06 (1).xlsx"))

    def test_old_style_conflict_copy_rejected(self):
        self.assertIsNone(os_._parse_xlsx_name("summary_PC17T033 (1).xlsx"))

    def test_unrelated_name_rejected(self):
        self.assertIsNone(os_._parse_xlsx_name("notes.xlsx"))
        self.assertIsNone(os_._parse_xlsx_name("summary_PC.csv"))


class TestAsInt(TempDataCase):
    def test_plain(self):
        self.assertEqual(os_._as_int("42"), 42)
        self.assertEqual(os_._as_int(42), 42)

    def test_whitespace_stripped(self):
        self.assertEqual(os_._as_int("  7  "), 7)

    def test_bad_value_returns_default(self):
        self.assertEqual(os_._as_int("abc"), 0)
        self.assertEqual(os_._as_int(None), 0)
        self.assertEqual(os_._as_int("abc", default=-1), -1)

    def test_float_string_is_not_an_int(self):
        # int("3.5") 會拋 ValueError -> 回預設。鎖定現行行為。
        self.assertEqual(os_._as_int("3.5"), 0)


class TestColMap(TempDataCase):
    def test_exact_lowercase_match(self):
        cm = os_._col_map(["idx", "patent", "status", "hits", "bytes", "code"])
        self.assertEqual(cm, {"idx": 0, "patent": 1, "status": 2,
                              "hits": 3, "bytes": 4, "code": 5})

    def test_case_and_whitespace_insensitive(self):
        cm = os_._col_map([" IDX ", "Patent", "STATUS"])
        self.assertEqual(cm["idx"], 0)
        self.assertEqual(cm["patent"], 1)
        self.assertEqual(cm["status"], 2)

    def test_substring_fallback(self):
        cm = os_._col_map(["row_idx", "patent_no", "http_status"])
        self.assertEqual(cm["idx"], 0)
        self.assertEqual(cm["patent"], 1)
        self.assertEqual(cm["status"], 2)

    def test_missing_columns_absent_from_result(self):
        cm = os_._col_map(["idx", "patent"])
        self.assertNotIn("hits", cm)
        self.assertNotIn("code", cm)

    def test_empty_header(self):
        self.assertEqual(os_._col_map([]), {})
        self.assertEqual(os_._col_map(None), {})
