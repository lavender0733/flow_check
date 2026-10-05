"""期別換算:公告期數(VOL)與日期互換,以及民國日期。純函式。

TWB(公告)一個月 3 期,落在 1、11、21 號;TWA(公開)一個月 2 期,落在 1、16 號。
期數 = 年份基準 × 100 + 當年第幾期。公式用 wiki 表格上的公告期數驗算過
(5328 = 2026-10-01、2419 = 2026-10-01、2413 = 2026-07-01)。
"""

from datetime import date

# kind → (起算年, 每月期數, 每期天數)
_KINDS = {
    "twb": (1973, 3, 10),
    "twa": (2002, 2, 15),
}


def _vol(kind: str, d: date) -> int:
    base, per_month, span = _KINDS[kind]
    return (d.year - base) * 100 + (d.month - 1) * per_month + (d.day - 1) // span + 1


def vol_twb(d: date) -> int:
    return _vol("twb", d)


def vol_twa(d: date) -> int:
    return _vol("twa", d)


def date_of_vol(kind: str, vol: int) -> date:
    """期數 → 那一期的日期(該期第一天)。kind 是 "twb" 或 "twa"。"""
    base, per_month, span = _KINDS[kind]
    vol = int(vol)
    n = vol % 100 - 1
    return date(vol // 100 + base, n // per_month + 1, (n % per_month) * span + 1)


def roc_to_date(s: str) -> date:
    """民國日期字串 → date。"1151001" → 2026-10-01。"""
    s = str(s).strip()
    return date(int(s[:-4]) + 1911, int(s[-4:-2]), int(s[-2:]))
