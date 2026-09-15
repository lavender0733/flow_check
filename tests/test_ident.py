"""機器身分與驗證碼遮罩。"""

from unittest.mock import patch

from _util import TempDataCase

from common import ident


class TestMachineId(TempDataCase):
    def test_plain_hostname(self):
        with patch.object(ident.socket, "gethostname", return_value="PC17T033"):
            self.assertEqual(ident.machine_id(), "PC17T033")

    def test_unsafe_characters_become_hyphen(self):
        with patch.object(ident.socket, "gethostname", return_value="my pc.local"):
            self.assertEqual(ident.machine_id(), "my-pc-local")

    def test_leading_trailing_separators_trimmed(self):
        with patch.object(ident.socket, "gethostname", return_value=".home."):
            self.assertEqual(ident.machine_id(), "home")

    def test_existing_hyphen_preserved(self):
        with patch.object(ident.socket, "gethostname", return_value="DESKTOP-V33GG01"):
            self.assertEqual(ident.machine_id(), "DESKTOP-V33GG01")

    def test_empty_hostname_falls_back(self):
        with patch.object(ident.socket, "gethostname", return_value=""):
            self.assertEqual(ident.machine_id(), "PC")

    def test_all_unsafe_falls_back(self):
        with patch.object(ident.socket, "gethostname", return_value="..."):
            self.assertEqual(ident.machine_id(), "PC")

    def test_result_is_filename_safe(self):
        """結果會被嵌進檔名（state_<機器>.json），不可含路徑分隔字元。"""
        with patch.object(ident.socket, "gethostname", return_value="a/b\\c:d"):
            mid = ident.machine_id()
        for ch in "/\\:*?\"<>|":
            self.assertNotIn(ch, mid)


class TestMaskCode(TempDataCase):
    def test_masks_after_four_preserving_length(self):
        self.assertEqual(ident.mask_code("21dcBD8B96d02A77"), "21dc" + "*" * 12)

    def test_four_or_fewer_unchanged(self):
        self.assertEqual(ident.mask_code("abcd"), "abcd")
        self.assertEqual(ident.mask_code("ab"), "ab")

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(ident.mask_code("  abcdef  "), "abcd**")

    def test_empty_and_none(self):
        self.assertEqual(ident.mask_code(""), "")
        self.assertEqual(ident.mask_code(None), "")

    def test_matches_the_two_display_facing_implementations(self):
        """額度卡與月進度卡的遮罩必須一致，統一版要與它們相同。"""
        from core import monthly_progress as mp
        from core import quota
        for code in ("21dcBD8B96d02A77", "abcd", "ab", "", "  abcdef  "):
            self.assertEqual(ident.mask_code(code), mp.mask_code(code))
            self.assertEqual(ident.mask_code(code), quota._mask_code(code))
