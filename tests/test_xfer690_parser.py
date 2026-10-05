import unittest

import _util  # noqa: F401  (sys.path)
from xfer690.parser import log_text, parse_raw, parse_subject
from xfer690.service import DEFAULT_XFER_CONFIG
from xfer690_mail import fixture_log, make_eml, subject

CFG = DEFAULT_XFER_CONFIG
DATE = "Thu, 01 Oct 2026 02:10:02 +0800"


class SubjectTest(unittest.TestCase):
    def test_subject_child_step(self):
        ev = parse_subject("🟢【SUCCESS】[s690] 012002-techreport-pdf-step1-raw-check | 01-tw/20-techreport-pdf",
                           DATE, "m1", CFG)
        self.assertEqual(ev["code"], "012002")
        self.assertEqual(ev["name"], "techreport-pdf-step1-raw-check")
        self.assertEqual(ev["group"], "01-tw/20-techreport-pdf")
        self.assertEqual(ev["status"], "ok")
        self.assertEqual(ev["id"], "m1")
        self.assertFalse(ev["raw"])

    def test_subject_parent_job(self):
        ev = parse_subject("🔴【FAILURE】[s690] 012000-techreport-pdf-auto | 01-tw/00-auto-schedule",
                           DATE, "m2", CFG)
        self.assertEqual(ev["code"], "012000")
        self.assertEqual(ev["status"], "fail")
        self.assertEqual(ev["status_word"], "FAILURE")

    def test_at_from_date_header(self):
        a = parse_subject("x", "Thu, 01 Oct 2026 02:10:02 +0800", "m", CFG)["at"]
        b = parse_subject("x", "Wed, 30 Sep 2026 18:10:02 +0000", "m", CFG)["at"]
        self.assertEqual(a, "2026-10-01T02:10:02+08:00")
        self.assertEqual(b, a)

    def test_unknown_status_word(self):
        ev = parse_subject("【ABORTED】[s690] 012001-techreport-pdf-step1-raw | g", DATE, "m", CFG)
        self.assertEqual(ev["code"], "012001")
        self.assertEqual(ev["status"], "unknown")

    def test_unrelated_mail_has_no_code(self):
        for s in ("🟢【SUCCESS】[s680] 012001-x | g", "週報", ""):
            ev = parse_subject(s, DATE, "m", CFG)
            self.assertIsNone(ev["code"], s)
            self.assertEqual(ev["status"], "unknown")


class RawTest(unittest.TestCase):
    def _base(self, status, code, name):
        return parse_subject(subject(status, code, name), DATE, "m", CFG)

    def test_real_failure_log_is_nodata(self):
        base = self._base("FAILURE", "012001", "techreport-pdf-step1-raw")
        ev = parse_raw(make_eml(base["subject"], log=fixture_log("012001")), base, CFG)
        self.assertTrue(ev["raw"])
        self.assertEqual(ev["status"], "nodata")
        self.assertEqual(ev["data"], {"VOL_DATE": "1151001"})
        self.assertIn("GCS 資料夾無檔案", ev["error"])
        self.assertEqual(ev["exec_id"], 2177)
        self.assertTrue(ev["url"].endswith("/execution/show/2177"))

    def test_real_success_log(self):
        base = self._base("SUCCESS", "010203", "gazette-biblio-step2-l1")
        ev = parse_raw(make_eml(base["subject"], log=fixture_log("010203")), base, CFG)
        self.assertEqual(ev["status"], "ok")
        self.assertEqual(ev["data"], {"VOL": "5328", "TAG": "20261001"})
        self.assertEqual(ev["writes"], 1640)
        self.assertEqual(ev["error"], "")

    def test_success_without_job_complete(self):
        base = self._base("SUCCESS", "990107", "pg-restore-point")
        ev = parse_raw(make_eml(base["subject"], log="restore point created\n"), base, CFG)
        self.assertEqual(ev["status"], "ok")
        self.assertIsNone(ev["writes"])
        self.assertEqual(ev["error"], "")

    def test_writes_takes_last_job_complete(self):
        log = ("RUNDECK:DATA:VOL=5328\n"
               "x [job_complete] gazette-sud-step1-raw 3563/3563 writes=3563 fail=0\n"
               "x [job_complete] gazette-sud-step2-l1 3561/3561 writes=3561 fail=0\n")
        base = self._base("SUCCESS", "011103", "gazette-sud-step2-l1")
        self.assertEqual(parse_raw(make_eml(base["subject"], log=log), base, CFG)["writes"], 3561)

    def test_nodata_only_on_raw_step(self):
        base = self._base("FAILURE", "012005", "techreport-pdf-step3-l3")
        ev = parse_raw(make_eml(base["subject"], log=fixture_log("012001")), base, CFG)
        self.assertEqual(ev["status"], "fail")

    def test_no_log_attachment(self):
        base = self._base("FAILURE", "012000", "techreport-pdf-auto")
        ev = parse_raw(make_eml(base["subject"]), base, CFG)
        self.assertEqual(ev["data"], {})
        self.assertIsNone(ev["writes"])
        self.assertEqual(ev["status"], "fail")

    def test_log_text_returns_attachment(self):
        raw = make_eml("x", log=fixture_log("012001"))
        self.assertIn("error: GCS 資料夾無檔案", log_text(raw))
        self.assertEqual(log_text(make_eml("x")), "")


if __name__ == "__main__":
    unittest.main()
