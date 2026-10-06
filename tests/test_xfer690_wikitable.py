import unittest
from datetime import datetime

import _util  # noqa: F401  (sys.path)
from core.paths import XFER690_CONFIG_PATH
from xfer690.parser import TPE
from xfer690.service import load_xfer_config
from xfer690.status import build_status
from xfer690.wiki import plan_fills, process_page
from xfer690.wikitable import (LabelMismatch, TableNotFound, apply_edits, apply_fills,
                               read_rows, split_row)
from xfer690_mail import FIX

CFG = load_xfer_config(XFER690_CONFIG_PATH)
WIKI = CFG["wiki"]
PH = WIKI["placeholder_re"]


def target(page, system):
    return next(t for t in WIKI["tables"] if t["page"] == page and t["system"] == system)


def text(name):
    return (FIX / name).read_bytes().decode("utf-8")


FULL = text("wiki_full.md")
DAILY = text("wiki_daily.md")
BIBLIO = text("wiki_biblio.md")
T_FULL_A = target("TW全文Full", "twa-full")
T_FULL_B = target("TW全文Full", "twb-full")
T_BIB_A = target("TW書目Biblio及雜項", "twa-biblio")
T_BIB_B = target("TW書目Biblio及雜項", "twb-biblio")
T_TECH = target("每日執行項目", "techreport")


def changed(a, b):
    la, lb = a.splitlines(keepends=True), b.splitlines(keepends=True)
    return [(i, x, y) for i, (x, y) in enumerate(zip(la, lb)) if x != y]


def row_of(content, key, key_col=2):
    for line in content.splitlines():
        cells = split_row(line) if line.lstrip().startswith("|") else []
        if len(cells) > key_col and cells[key_col].strip() == key:
            return [c.strip() for c in cells]
    return None


class TableTest(unittest.TestCase):
    def test_split_row_keeps_spacing_and_escaped_pipe(self):
        self.assertEqual(split_row("| a | b\\|c |  d  |\n"), [" a ", " b\\|c ", "  d  "])
        self.assertEqual(split_row("|x|y|"), ["x", "y"])

    def test_full_twa_keyed_by_vol(self):
        out, rep = apply_fills(FULL, T_FULL_A, {"2414": {"cells": {4: "v"}}}, PH)
        self.assertEqual(rep["filled"], 1)
        self.assertEqual(row_of(out, "2414")[4], "v")
        self.assertEqual(row_of(out, "2413")[4], "")

    def test_only_target_lines_change(self):
        out, _ = apply_fills(FULL, T_FULL_A, {"2414": {"cells": {4: "v", 5: "v"}}}, PH)
        diff = changed(FULL, out)
        self.assertEqual(len(diff), 1)
        self.assertIn("2414", diff[0][1])
        self.assertEqual(len(out.splitlines()), len(FULL.splitlines()))
        # 寬度不變
        self.assertEqual(len(diff[0][1]), len(diff[0][2]))

    def test_never_overwrites_filled_cell(self):
        out, rep = apply_fills(FULL, T_FULL_A, {"2411": {"cells": {4: "v"}}}, PH)
        self.assertEqual(out, FULL)
        self.assertEqual(rep["conflicts"], [{"key": "2411", "col": 4, "existing": "2153", "wanted": "v"}])

    def test_placeholder_treated_empty(self):
        out, rep = apply_fills(FULL, T_FULL_A, {"2413": {"cells": {3: "20260930"}}}, PH)
        self.assertEqual(row_of(out, "2413")[3], "20260930")
        self.assertEqual(rep["filled"], 1)

    def test_same_value_is_noop(self):
        out, rep = apply_fills(FULL, T_FULL_A, {"2409": {"cells": {4: "v"}}}, PH)
        self.assertEqual(out, FULL)
        self.assertEqual((rep["filled"], rep["conflicts"]), (0, []))

    def test_twb_table_selected_by_after(self):
        out, _ = apply_fills(FULL, T_FULL_B, {"5320": {"cells": {4: "v"}}}, PH)
        self.assertEqual(row_of(out, "5320")[4], "v")
        self.assertEqual(changed(FULL, out)[0][0], FULL.splitlines().index(
            next(l for l in FULL.splitlines() if "| 5320 |" in l)))

    def test_skips_commented_row(self):
        # 註解裡的範例列 "| 1150701A13X01 | 20260701 | 07/01 | 2413 |" 不能被當成表格
        out, rep = apply_fills(FULL, T_FULL_A, {"2413": {"cells": {4: "v"}}}, PH)
        diff = changed(FULL, out)
        self.assertEqual(len(diff), 1)
        self.assertNotIn("1150701A13X01 | 20260701", diff[0][1])
        self.assertIn("<!-- copy & paste", out)

    def test_label_mismatch_refuses_table(self):
        bad = dict(T_FULL_A, cols={"4": {"label": "L1", "value": "v", "step": "011603"}})
        with self.assertRaises(LabelMismatch):
            apply_fills(FULL, bad, {"2414": {"cells": {4: "v"}}}, PH)

    def test_daily_column_shift_refuses_every_target(self):
        # 每日表 RAW/L3/OS 重複四次:在日期後面插一組新系統,每個系統的 target 都要拒寫(不能只看自己那幾欄的欄名)
        lines = DAILY.split("\n")
        i = next(n for n, l in enumerate(lines) if l.startswith("|      | RAW"))
        lines[i] = lines[i].replace("|      |", "|      | RAW | L3 | OS | 案例 |", 1)
        shifted = "\n".join(lines)
        for t in [x for x in WIKI["tables"] if x["page"] == "每日執行項目"]:
            with self.assertRaises(LabelMismatch, msg=t["system"]):
                apply_fills(shifted, t, {"2026/08/28": {"cells": {int(c): "v" for c in t["cols"]}}}, PH)

    def test_edit_refused_after_column_insert(self):
        # 在 RAW 前面加一欄:使用者對 RAW 的修改不能寫進新欄
        shifted = FULL.replace("| 資料包 TAG | RAW |", "| 資料包 TAG | QA | RAW |", 1)
        with self.assertRaises(LabelMismatch):
            apply_edits(shifted, T_FULL_A, [{"key": "2414", "col": 4, "expect": "", "value": "v"}])

    def test_daily_uses_first_row_labels(self):
        out, rep = apply_fills(DAILY, T_TECH, {"2026/08/28": {"cells": {9: "v"}}}, PH)
        self.assertEqual(out, DAILY)    # 原本就是 v
        self.assertEqual(rep["conflicts"], [])

    def test_insert_row_daily_in_order(self):
        out, rep = apply_fills(DAILY, T_TECH, {"2026/10/01": {"cells": {9: "v", 10: "v", 11: "v"},
                                                             "new": {0: "2026/10/01"}}}, PH)
        self.assertEqual(rep["inserted"], 1)
        lines = out.splitlines()
        i = next(n for n, l in enumerate(lines) if "2026/10/01" in l)
        self.assertIn("RAW | L3 | OS | 案例", lines[i - 1])
        self.assertIn("2026/08/28", lines[i + 1])
        self.assertEqual(len(split_row(lines[i])), 15)
        self.assertEqual([c.strip() for c in split_row(lines[i])][9:12], ["v", "v", "v"])
        self.assertEqual(len(lines), len(DAILY.splitlines()) + 1)

    def test_older_than_oldest_row_skipped(self):
        out, rep = apply_fills(BIBLIO, T_BIB_B, {"5211": {"cells": {4: "1819"}}}, PH)
        self.assertEqual(out, BIBLIO)
        self.assertEqual((rep["inserted"], rep["skipped"]), (0, ["5211"]))

    def test_insert_row_biblio_with_new(self):
        out, rep = apply_fills(BIBLIO, T_BIB_A, {"2420": {"cells": {3: "20261016"},
                                                       "new": {0: "20261016", 1: "10/16", 2: "2420"}}}, PH)
        self.assertEqual(rep["inserted"], 1)
        lines = out.splitlines()
        i = next(n for n, l in enumerate(lines) if "| 2420 |" in l)
        self.assertIn("| 2419 |", lines[i + 1])
        self.assertEqual([c.strip() for c in split_row(lines[i])][:4], ["20261016", "10/16", "2420", "20261016"])

    def test_biblio_existing_row_writes_noop(self):
        out, rep = apply_fills(BIBLIO, T_BIB_A, {"2419": {"cells": {4: "2384"}}}, PH)
        self.assertEqual(out, BIBLIO)
        self.assertEqual(rep["filled"], 0)

    def test_table_not_found(self):
        with self.assertRaises(TableNotFound):
            apply_fills("# empty\n", T_FULL_A, {}, PH)
        with self.assertRaises(TableNotFound):
            apply_fills(FULL, dict(T_FULL_A, after="不存在的標題"), {}, PH)

    def test_crlf_preserved(self):
        crlf = FULL.replace("\n", "\r\n")
        out, _ = apply_fills(crlf, T_FULL_A, {"2414": {"cells": {4: "v"}}}, PH)
        self.assertEqual(out.count("\r\n"), crlf.count("\r\n"))
        self.assertEqual(len(changed(crlf, out)), 1)

    def test_read_rows(self):
        r = read_rows(BIBLIO, T_BIB_A)
        self.assertEqual(r["rows"]["2419"][4], "2384")
        self.assertEqual(r["labels"][5], "Gazette SUD")

    def test_edit_overwrites_when_expect_matches(self):
        out, rep = apply_edits(FULL, T_FULL_A, [{"key": "2414", "col": 1, "expect": "07/11", "value": "07/16"}])
        self.assertEqual(rep, {"edited": 1, "conflicts": []})
        self.assertEqual(row_of(out, "2414")[1], "07/16")
        self.assertEqual(len(changed(FULL, out)), 1)

    def test_edit_refused_when_page_changed(self):
        out, rep = apply_edits(FULL, T_FULL_A, [{"key": "2414", "col": 1, "expect": "07/10", "value": "07/16"}])
        self.assertEqual(out, FULL)
        self.assertEqual(rep["edited"], 0)
        self.assertEqual(rep["conflicts"][0]["existing"], "07/11")


def ev(code, at, status="ok", data=None, writes=None):
    e = {"id": code + at, "code": code, "name": "", "group": "", "status": status,
         "at": f"2026-{at[:5]}T{at[6:]}:00+08:00", "subject": ""}
    if data is not None:
        e.update(raw=True, data=data, writes=writes, error="")
    return e


NOW = datetime(2026, 10, 1, 9, 0, tzinfo=TPE)


class PlanTest(unittest.TestCase):
    def status(self, evs, now=NOW):
        return build_status(evs, CFG["systems"], now, 14)

    def test_plan_v_requires_check(self):
        st = self.status([ev("012001", "10-01 02:10")], datetime(2026, 10, 1, 3, 0, tzinfo=TPE))
        self.assertEqual(plan_fills(st, T_TECH, WIKI), {})

    def test_plan_nodata_fills_dash(self):
        # 每日系統「GCS 資料夾無檔案」:三格填 -(人工登記的做法),不能打 v
        st = self.status([ev("012000", "10-01 02:10", "fail"),
                          ev("012001", "10-01 02:10", "nodata", {"VOL_DATE": "1151001"})])
        plan = plan_fills(st, T_TECH, WIKI)
        self.assertEqual(WIKI["nodata_value"], "-")
        self.assertEqual(plan["2026/10/01"]["cells"], {9: "-", 10: "-", 11: "-"})
        self.assertEqual(plan["2026/10/01"]["new"], {0: "2026/10/01"})

    def test_plan_nodata_value_from_config(self):
        st = self.status([ev("012000", "10-01 02:10", "fail"),
                          ev("012001", "10-01 02:10", "nodata", {"VOL_DATE": "1151001"})])
        plan = plan_fills(st, T_TECH, dict(WIKI, nodata_value="無資料"))
        self.assertEqual(plan["2026/10/01"]["cells"], {9: "無資料", 10: "無資料", 11: "無資料"})

    def test_nodata_does_not_overwrite_existing_v(self):
        # 頁面上已經有人打了 v(或程式之前打的):不自動覆蓋,列為衝突讓人看
        st = self.status([ev("012000", "08-28 02:10", "fail"),
                          ev("012001", "08-28 02:10", "nodata", {"VOL_DATE": "1150828"})],
                         datetime(2026, 8, 28, 9, 0, tzinfo=TPE))
        plan = plan_fills(st, T_TECH, WIKI)
        out, rep = apply_fills(DAILY, T_TECH, plan, PH)
        self.assertEqual(out, DAILY)
        self.assertEqual({c["col"] for c in rep["conflicts"]}, {9, 10, 11})

    def test_plan_writes_and_tag(self):
        steps = ["010301", "010302", "010303", "010304", "010305", "010306", "010307", "010308",
                 "010309", "010310", "010311", "010312", "010315", "010300"]
        evs = [ev(c, "10-01 00:30") for c in steps[1:]]
        evs.insert(0, ev("010301", "10-01 00:30", data={"VOL": "2419", "TAG": "20261001"}))
        evs[2] = ev("010303", "10-01 00:30", data={"VOL": "2419", "TAG": "20261001"}, writes=2384)
        plan = plan_fills(self.status(evs), T_BIB_A, WIKI)
        self.assertEqual(plan["2419"]["cells"], {3: "20261001", 4: "2384"})

    def test_plan_writes_only_when_whole_period_ok(self):
        evs = [ev("010301", "10-01 00:30", data={"VOL": "2419", "TAG": "20261001"}),
               ev("010303", "10-01 00:30", data={"VOL": "2419"}, writes=2384),
               ev("010305", "10-01 00:31", "fail")]
        plan = plan_fills(self.status(evs), T_BIB_A, WIKI)
        self.assertEqual(plan["2419"]["cells"], {3: "20261001"})

    def test_plan_new_row_template(self):
        evs = [ev("010301", "10-01 00:30", data={"VOL": "2420", "TAG": "20261016"})]
        plan = plan_fills(self.status(evs), T_BIB_A, WIKI)
        self.assertEqual(plan["2420"]["new"], {0: "20261016", 1: "10/16", 2: "2420"})

    def test_plan_vol_nodata_writes_nothing(self):
        # A4:期別系統的無資料 = 檔案還沒到,列入警示、不寫 wiki(連 TAG 跟新列都不要)
        st = self.status([ev("011601", "09-29 14:54", "nodata", {"VOL": "2415", "TAG": "20260929"})])
        self.assertEqual(plan_fills(st, T_FULL_A, WIKI), {})

    def test_plan_skips_missing_periods(self):
        self.assertEqual(plan_fills(self.status([]), T_TECH, WIKI), {})


class EditValidationTest(unittest.TestCase):
    TS = [T_FULL_A, T_FULL_B]

    def run_edits(self, edits):
        st = build_status([], CFG["systems"], NOW, 14)
        return process_page(FULL, self.TS, st, WIKI, edits)

    def test_numeric_col_refused(self):
        # 欄要用欄名:直接給欄號會略過欄名比對(可以寫到列鍵、資訊欄等任何一格)
        out, rep, _ = self.run_edits([{"table": "TWA", "key": "2414", "col": 2, "expect": "2414", "value": "9999"}])
        self.assertEqual(out, FULL)
        self.assertEqual(rep["edited"], 0)
        self.assertEqual(rep["conflicts"][0]["reason"], "找不到這一欄")

    def test_value_that_breaks_table_refused(self):
        for bad in ["07|16", "07\r16", "07\n| x | y |"]:
            out, rep, _ = self.run_edits([{"table": "TWA", "key": "2414", "col": "日期",
                                           "expect": "07/11", "value": bad}])
            self.assertEqual(out, FULL, repr(bad))
            self.assertEqual(rep["edited"], 0, repr(bad))
            self.assertEqual(rep["conflicts"][0]["reason"], "值不能含 | 或換行", repr(bad))

    def test_user_clear_not_refilled_in_same_write(self):
        # 使用者清掉程式填的值:同一次寫入的自動填格不能又把它填回去
        st = build_status([ev(c, "10-01 02:10") for c in
                           ["012001", "012002", "012003", "012004", "012005", "012006", "012007", "012008", "012000"]],
                          CFG["systems"], NOW, 14)
        daily = [t for t in WIKI["tables"] if t["page"] == "每日執行項目"]
        filled, _, _ = process_page(DAILY, daily, st, WIKI)
        self.assertEqual(row_of(filled, "2026/10/01", 0)[9], "v")
        out, rep, _ = process_page(filled, daily, st, WIKI, [
            {"table": "", "key": "2026/10/01", "col": "RAW", "system": "techreport",
             "expect": "v", "value": ""}])
        self.assertEqual(rep["edited"], 1)
        self.assertEqual(row_of(out, "2026/10/01", 0)[9], "")
        self.assertEqual(row_of(out, "2026/10/01", 0)[10], "v")    # 別格照填

    def test_label_edit_still_works(self):
        out, rep, _ = self.run_edits([{"table": "TWA", "key": "2414", "col": "日期",
                                       "expect": "07/11", "value": "07/16"}])
        self.assertEqual(rep["edited"], 1)
        self.assertEqual(row_of(out, "2414")[1], "07/16")


if __name__ == "__main__":
    unittest.main()
