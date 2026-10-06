import re
import unittest

from _util import ROOT


class BatTest(unittest.TestCase):
    def test_bat_files_have_no_control_bytes(self):
        for p in ROOT.glob("*.bat"):
            data = p.read_bytes()
            bad = [b for b in data if b < 0x20 and b not in (0x09, 0x0A, 0x0D)]
            self.assertEqual(bad, [], p.name)

    def test_authorize_bat_points_at_script(self):
        text = (ROOT / "690授權.bat").read_text(encoding="utf-8")
        m = re.search(r"python (\S+\.py)", text)
        self.assertIsNotNone(m)
        self.assertTrue((ROOT / m.group(1).replace("\\", "/")).exists(), m.group(1))


if __name__ == "__main__":
    unittest.main()
