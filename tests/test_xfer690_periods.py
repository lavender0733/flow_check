import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "program"))

from xfer690.periods import date_of_vol, roc_to_date, vol_twa, vol_twb


class PeriodsTest(unittest.TestCase):
    def test_vol_twb_matches_wiki(self):
        self.assertEqual(vol_twb(date(2026, 10, 1)), 5328)
        self.assertEqual(vol_twb(date(2026, 9, 21)), 5327)

    def test_vol_twa_matches_wiki(self):
        self.assertEqual(vol_twa(date(2026, 10, 1)), 2419)
        self.assertEqual(vol_twa(date(2026, 9, 16)), 2418)
        self.assertEqual(vol_twa(date(2026, 7, 1)), 2413)

    def test_date_of_vol_roundtrip(self):
        d = date(2025, 1, 1)
        n = 0
        while d <= date(2027, 12, 31):
            if d.day in (1, 11, 21):
                self.assertEqual(date_of_vol("twb", vol_twb(d)), d)
                n += 1
            if d.day in (1, 16):
                self.assertEqual(date_of_vol("twa", vol_twa(d)), d)
                n += 1
            d += timedelta(days=1)
        self.assertEqual(n, 36 * 5)

    def test_roc_to_date(self):
        self.assertEqual(roc_to_date("1151001"), date(2026, 10, 1))


if __name__ == "__main__":
    unittest.main()
