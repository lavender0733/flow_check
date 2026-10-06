import unittest
from datetime import datetime

import _util  # noqa: F401  (sys.path)
from core.paths import XFER690_CONFIG_PATH
from xfer690.parser import TPE
from xfer690.service import load_xfer_config
from xfer690.status import build_status, cell_done, group_runs, period_of, step_pairs

CFG = load_xfer_config(XFER690_CONFIG_PATH)
SYS = {s["id"]: s for s in CFG["systems"]}
NOW = datetime(2026, 10, 1, 9, 0, tzinfo=TPE)

_n = [0]


def ev(code, at, status="ok", data=None, name="", writes=None):
    """at 是 "MM-DD HH:MM",年份固定 2026。"""
    _n[0] += 1
    e = {"id": f"m{_n[0]}", "code": code, "name": name or f"x-{code}", "group": "g",
         "status": status, "at": f"2026-{at[:5]}T{at[6:]}:00+08:00", "subject": "", "raw": data is not None}
    if data is not None:
        e.update(data=data, writes=writes, error="")
    return e


def system(st, sid):
    return next(s for s in st["systems"] if s["id"] == sid)


def period(st, sid, key):
    return next(p for p in system(st, sid)["periods"] if p["key"] == key)


def full_twa_0930():
    """09-30 14:24–14:30 全文 TWA 依序跑了 2415 → 2414 → 2413,只有三封 011601 下載了 log。"""
    out, minute = [], 24
    for vol in ("2415", "2414", "2413"):
        for code in ("011601", "011602", "011603", "011604", "011605", "011606"):
            data = {"VOL": vol, "TAG": "20260930"} if code == "011601" else None
            out.append(ev(code, f"09-30 14:{minute:02d}", data=data))
        minute += 2
    return out


class PeriodTest(unittest.TestCase):
    def test_daily_period_is_run_date(self):
        # 規則:每日系統以執行日期為一期(log 的 DOWNLOAD_DATE 不採用,見 ledger)
        e = ev("011901", "10-01 06:10", data={"DOWNLOAD_DATE": "20260930"})
        self.assertEqual(period_of(e, SYS["legal"], None), "2026-10-01")

    def test_daily_period_fallback_at(self):
        self.assertEqual(period_of(ev("012001", "10-01 02:10"), SYS["techreport"], None), "2026-10-01")

    def test_vol_from_log_not_date(self):
        e = ev("011601", "09-30 14:28", data={"VOL": "2413"})
        self.assertEqual(period_of(e, SYS["twa-full"], None), "2413")
        st = build_status([e], CFG["systems"], NOW, 14)
        self.assertEqual(period(st, "twa-full", "2413")["date"], "2026-07-01")

    def test_run_period_inherited(self):
        evs = [ev("010301", "10-01 00:30", data={"VOL": "2419", "TAG": "20261001"}),
               ev("010302", "10-01 00:30"), ev("010303", "10-01 00:31"), ev("010304", "10-01 00:31")]
        p = period(build_status(evs, CFG["systems"], NOW, 14), "twa-biblio", "2419")
        self.assertEqual(sorted(p["steps"])[:4], ["010301", "010302", "010303", "010304"])
        self.assertEqual(p["tag"], "20261001")

    def test_runs_split_by_raw_step(self):
        runs = group_runs(full_twa_0930(), SYS["twa-full"])
        self.assertEqual([len(r) for r in runs], [6, 6, 6])
        self.assertEqual([r[0]["data"]["VOL"] for r in runs], ["2415", "2414", "2413"])
        st = build_status(full_twa_0930(), CFG["systems"], NOW, 14)
        self.assertEqual({p["key"] for p in system(st, "twa-full")["periods"]}, {"2415", "2414", "2413"})

    def test_split_by_gap(self):
        # 書目 RAW 09-30 14:31 手動跑,L1 以後隔天 00:30 才跑:相隔超過 2 小時是另一段
        evs = [ev("010201", "09-30 14:31", data={"VOL": "5328"}), ev("010202", "09-30 14:32"),
               ev("010203", "10-01 00:30", data={"VOL": "5328"}), ev("010204", "10-01 00:30")]
        self.assertEqual([len(r) for r in group_runs(evs, SYS["twb-biblio"])], [2, 2])


class StateTest(unittest.TestCase):
    def test_vol_nodata_is_alert(self):
        evs = [ev("011601", "09-29 14:54", status="nodata", data={"VOL": "2415"})]
        st = build_status(evs, CFG["systems"], NOW, 14)
        p = period(st, "twa-full", "2415")
        self.assertEqual(p["state"], "fail")
        self.assertFalse(cell_done(p, "011601", "011602"))
        self.assertGreaterEqual(system(st, "twa-full")["alerts"], 1)

    def test_latest_run_wins(self):
        evs = [ev("011601", "09-29 14:54", status="nodata", data={"VOL": "2415"})] + full_twa_0930()[:6]
        p = period(build_status(evs, CFG["systems"], NOW, 14), "twa-full", "2415")
        self.assertEqual(p["steps"]["011601"]["state"], "ok")
        self.assertEqual(p["steps"]["011601"]["runs"], 2)
        self.assertEqual(p["state"], "ok")
        self.assertTrue(cell_done(p, "011601", "011602"))

    def test_check_failure_overrides_step(self):
        evs = [ev("012001", "10-01 02:10"), ev("012002", "10-01 02:10", status="fail")]
        p = period(build_status(evs, CFG["systems"], NOW, 14), "techreport", "2026-10-01")
        self.assertFalse(cell_done(p, "012001", "012002"))
        self.assertEqual(p["state"], "fail")

    def test_rerun_failure_after_check_not_done(self):
        # 步驟成功、check 成功,三小時後重跑步驟失敗:舊的 check 不能讓這格打 v
        evs = [ev("011601", "09-30 14:24", data={"VOL": "2415"}), ev("011602", "09-30 14:24"),
               ev("011601", "09-30 17:30", status="fail", data={"VOL": "2415"})]
        p = period(build_status(evs, CFG["systems"], NOW, 14), "twa-full", "2415")
        self.assertEqual(p["steps"]["011601"]["state"], "fail")
        self.assertFalse(cell_done(p, "011601", "011602"))

    def test_step_ok_check_pending(self):
        evs = [ev("012001", "10-01 02:10")]
        p = period(build_status(evs, CFG["systems"], datetime(2026, 10, 1, 3, 0, tzinfo=TPE), 14),
                   "techreport", "2026-10-01")
        self.assertFalse(cell_done(p, "012001", "012002"))
        self.assertEqual(p["state"], "pending")

    def test_nodata_marks_whole_period(self):
        evs = [ev("012000", "10-01 02:10", status="fail"),
               ev("012001", "10-01 02:10", status="nodata", data={"VOL_DATE": "1151001"})]
        st = build_status(evs, CFG["systems"], NOW, 14)
        p = period(st, "techreport", "2026-10-01")
        self.assertEqual(p["state"], "nodata")
        for step, chk in (("012001", "012002"), ("012005", "012006"), ("012007", "012008")):
            self.assertTrue(cell_done(p, step, chk), step)
        self.assertEqual(system(st, "techreport")["alerts"],
                         sum(1 for q in system(st, "techreport")["periods"] if q["state"] == "missing"))
        self.assertNotIn(p["state"], ("fail", "missing", "unknown"))

    def test_missing_after_deadline(self):
        st = build_status([], CFG["systems"], NOW, 14)
        self.assertEqual(period(st, "techreport", "2026-10-01")["state"], "missing")

    def test_pending_before_deadline(self):
        st = build_status([], CFG["systems"], datetime(2026, 10, 1, 3, 0, tzinfo=TPE), 14)
        self.assertEqual(period(st, "techreport", "2026-10-01")["state"], "pending")

    def test_partial_run_missing_steps(self):
        evs = [ev("011901", "10-01 06:10", data={"DOWNLOAD_DATE": "20260930"}, writes=16556),
               ev("011902", "10-01 06:10")]
        st = build_status(evs, CFG["systems"], datetime(2026, 10, 2, 9, 0, tzinfo=TPE), 14)
        p = period(st, "legal", "2026-10-01")
        for code in ("011903", "011909", "011914"):
            self.assertEqual(p["steps"][code]["state"], "missing", code)
        self.assertEqual(p["state"], "missing")
        self.assertTrue(cell_done(p, "011901", "011902"))
        self.assertFalse(cell_done(p, "011909", "011910"))
        self.assertGreaterEqual(system(st, "legal")["alerts"], 1)

    def test_steps_after_failure_did_not_run(self):
        evs = [ev("011701", "10-01 06:10"), ev("011702", "10-01 06:10"),
               ev("011703", "10-01 06:10", status="fail")]
        p = period(build_status(evs, CFG["systems"], NOW, 14), "status", "2026-10-01")
        self.assertEqual(p["state"], "fail")
        self.assertEqual(p["steps"]["011707"]["state"], "none")

    def test_vol_expected_from_schedule(self):
        st = build_status([ev("012001", "09-10 02:10")], CFG["systems"], NOW, 20)
        keys = {p["key"]: p["state"] for p in system(st, "twa-biblio")["periods"]}
        self.assertEqual(keys, {"2419": "missing", "2418": "missing"})
        twb = {p["key"] for p in system(st, "twb-biblio")["periods"]}
        self.assertEqual(twb, {"5328", "5327", "5326"})

    def test_no_expectation_before_first_mail(self):
        # 信從 09-29 下午才開始有:期限在第一封信之前的日子 / 期別不算「該到沒到」
        st = build_status([ev("990107", "09-29 14:24")], CFG["systems"], NOW, 14)
        days = {p["key"] for p in system(st, "techreport")["periods"]}
        self.assertEqual(days, {"2026-09-30", "2026-10-01"})
        self.assertEqual({p["key"] for p in system(st, "twa-biblio")["periods"]}, {"2419"})

    def test_manual_never_missing(self):
        st = build_status([], CFG["systems"], NOW, 14)
        self.assertEqual(system(st, "twa-full")["periods"], [])
        self.assertEqual(system(st, "eng")["periods"], [])

    def test_old_vol_rerun_listed(self):
        # 09-30 補跑 2025 年的 5211:收信時間在一個月內,照樣列出
        evs = [ev("010201", "09-30 15:38", data={"VOL": "5211"}, writes=1819)]
        p = period(build_status(evs, CFG["systems"], NOW, 14), "twb-biblio", "5211")
        self.assertEqual(p["date"], "2025-04-11")

    def test_unconfigured_goes_to_others(self):
        evs = [ev("990107", "10-01 06:30", status="fail"), ev("990107", "10-01 06:30"),
               ev("990107", "09-30 06:30")]
        st = build_status(evs, CFG["systems"], NOW, 14)
        m = system(st, "990107")
        self.assertFalse(m["configured"])
        self.assertEqual(m["kind"], "maint")
        self.assertEqual(len(m["periods"]), 1)
        self.assertEqual(m["alerts"], 0)

    def test_adhoc_failure_alerts(self):
        evs = [ev("014101", "10-01 08:20", name="legal-backfill-step1-raw"),
               ev("014102", "10-01 08:21", status="fail", name="legal-backfill-step2-l1")]
        st = build_status(evs, CFG["systems"], NOW, 14)
        a = system(st, "0141xx")
        self.assertEqual(a["kind"], "adhoc")
        self.assertEqual(len(a["periods"]), 1)
        self.assertEqual(a["periods"][0]["state"], "fail")
        self.assertEqual(a["alerts"], 1)
        self.assertEqual(a["names"]["014102"], "legal-backfill-step2-l1")

    def test_alerts_total(self):
        st = build_status([], CFG["systems"], NOW, 1)
        self.assertEqual(st["alerts"], sum(s["alerts"] for s in st["systems"]))

    def test_names_from_any_period(self):
        # 今天沒來信的步驟,名稱取自之前的信(畫面上才不會只顯示代碼)
        evs = [ev("012003", "09-30 02:10", name="techreport-pdf-step2-l1"),
               ev("012001", "10-01 02:10", status="nodata", data={}, name="techreport-pdf-step1-raw")]
        names = system(build_status(evs, CFG["systems"], NOW, 14), "techreport")["names"]
        self.assertEqual(names["012003"], "techreport-pdf-step2-l1")

    def test_step_pairs(self):
        self.assertEqual(step_pairs(["012001", "012002", "012003", "012004"]),
                         [["012001", "012002"], ["012003", "012004"]])
        self.assertEqual(step_pairs(["010213", "010215"]), [["010213", None], ["010215", None]])


if __name__ == "__main__":
    unittest.main()
