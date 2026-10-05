import json
import unittest
from datetime import datetime

from _util import TempDataCase
from core.paths import XFER690_CONFIG_PATH
from xfer690.gmail_client import AuthError, GmailError
from xfer690.parser import TPE
from xfer690.service import NotLoggedIn, Xfer690Service
from xfer690.wiki import WikiClient, WikiError, sync
from xfer690_mail import FIX, fixture_log, make_eml, subject

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=TPE)
REAL_CFG = json.loads(XFER690_CONFIG_PATH.read_text(encoding="utf-8"))


class FakeGmail:
    """messages: {id: (subject, date, size, log or None)}"""

    def __init__(self, messages, fail=None):
        self.messages = messages
        self.fail = fail
        self.queries, self.meta_calls, self.raw_calls = [], [], []

    def list_ids(self, query, limit=1000):
        if self.fail:
            raise self.fail
        self.queries.append(query)
        return list(self.messages)

    def fetch_meta(self, mid):
        self.meta_calls.append(mid)
        s, d, size, _ = self.messages[mid]
        return {"subject": s, "date": d, "size": size}

    def fetch_raw(self, mid):
        self.raw_calls.append(mid)
        s, d, _, log = self.messages[mid]
        return make_eml(s, log=log, date=d)


def mail(code, name, hhmm, status="SUCCESS", day="01 Oct", size=70000, log=None):
    return (subject(status, code, name), f"Thu, {day} 2026 {hhmm}:00 +0800", size, log)


class FakeWikiPost:
    """假的 Wiki.js GraphQL 伺服器:頁面放記憶體,記錄每次 update。"""

    def __init__(self, pages, fail_pages=(), jwt="JWT1"):
        self.pages = dict(pages)        # path 結尾頁名 → content
        self.fail_pages = set(fail_pages)
        self.updates = []
        self.jwt = jwt
        self.reject_token = False

    def __call__(self, url, headers, body):
        req = json.loads(body)
        q, v = req["query"], req["variables"]
        if "authentication" in q:
            ok = v["u"] == "me" and v["p"] == "pw"
            return json.dumps({"data": {"authentication": {"login": {
                "responseResult": {"succeeded": ok, "message": "" if ok else "bad"},
                "jwt": self.jwt if ok else None}}}}).encode()
        if self.reject_token:
            return json.dumps({"errors": [{"message": "Forbidden"}]}).encode()
        if "singleByPath" in q:
            name = v["p"].rsplit("/", 1)[-1]
            if name in self.fail_pages:
                return json.dumps({"errors": [{"message": "boom"}]}).encode()
            return json.dumps({"data": {"pages": {"singleByPath": {
                "id": hash(name) % 9999, "content": self.pages[name], "title": name, "description": "",
                "editor": "markdown", "isPublished": True, "isPrivate": False, "locale": "zh-tw",
                "path": v["p"], "tags": [{"tag": "a"}], "scriptCss": "", "scriptJs": ""}}}}).encode()
        if "update(" in q:
            self.updates.append(v)
            self.pages[v["path"].rsplit("/", 1)[-1]] = v["content"]
            return json.dumps({"data": {"pages": {"update": {
                "responseResult": {"succeeded": True}}}}}).encode()
        raise AssertionError(q)


def wiki_pages():
    return {"TW書目Biblio及雜項": (FIX / "wiki_biblio.md").read_bytes().decode("utf-8"),
            "TW全文Full": (FIX / "wiki_full.md").read_bytes().decode("utf-8"),
            "每日執行項目": (FIX / "wiki_daily.md").read_bytes().decode("utf-8")}


class ServiceCase(TempDataCase):
    def make(self, gmail, wiki=None, cfg_patch=None, secrets=None, now=NOW):
        cfg = json.loads(json.dumps(REAL_CFG))
        cfg["wiki"].update(enabled=True)
        for k, v in (cfg_patch or {}).items():
            if k == "wiki":
                cfg["wiki"].update(v)
            else:
                cfg[k] = v
        cfgp = self.write_json("xfer690.json", cfg)
        cred = self.write_json("cred.json", {})
        sec = self.write_json("secrets.json", secrets if secrets is not None else
                              {"wiki_user": "me", "wiki_password": "pw"})
        self.wiki_post = wiki or FakeWikiPost(wiki_pages())
        self.clock = [now]

        def gf():
            if isinstance(gmail, Exception):
                raise gmail
            return gmail

        return Xfer690Service(config_path=cfgp, cache_path=self.tmp / "cache.json", secrets_path=sec,
                              credentials_path=cred, gmail_factory=gf,
                              wiki_factory=lambda url, token=None: WikiClient(url, token, post=self.wiki_post),
                              now=lambda: self.clock[0])


class PollTest(ServiceCase):
    def test_metadata_only_for_ok_daily(self):
        g = FakeGmail({"a": mail("012001", "techreport-pdf-step1-raw", "02:10"),
                       "b": mail("012002", "techreport-pdf-step1-raw-check", "02:10"),
                       "c": mail("012000", "techreport-pdf-auto", "02:11")})
        snap = self.make(g).poll()
        self.assertEqual(g.raw_calls, [])
        self.assertEqual(len(g.meta_calls), 3)
        self.assertEqual(snap["auth"], "ok")
        self.assertIn("newer_than:45d", g.queries[0])

    def test_raw_for_failure(self):
        g = FakeGmail({"a": mail("012001", "techreport-pdf-step1-raw", "02:10", "FAILURE",
                                 log=fixture_log("012001"))})
        snap = self.make(g).poll()
        self.assertEqual(g.raw_calls, ["a"])
        self.assertEqual(snap["recent"][0]["status"], "nodata")

    def test_raw_only_raw_step_for_vol(self):
        msgs = {}
        for i, vol in enumerate(("2415", "2414", "2413")):
            for j, code in enumerate(("011601", "011602", "011603", "011604", "011605", "011606")):
                log = f"RUNDECK:DATA:VOL={vol}\n" if code == "011601" else None
                msgs[f"f{i}{j}"] = mail(code, f"pub-fullimage-{code}", f"14:{24 + 2 * i:02d}",
                                        day="30 Sep", log=log)
        g = FakeGmail(msgs)
        snap = self.make(g).poll()
        self.assertEqual(sorted(g.raw_calls), ["f00", "f10", "f20"])
        twa = next(s for s in snap["status"]["systems"] if s["id"] == "twa-full")
        self.assertEqual({p["key"] for p in twa["periods"]}, {"2415", "2414", "2413"})

        g2 = FakeGmail({f"b{j}": mail(c, f"pub-gazette-biblio-{c}", "00:30")
                        for j, c in enumerate(["010301", "010302", "010303", "010304",
                                               "010305", "010306", "010307", "010308"])})
        self.make(g2).poll()
        self.assertEqual(sorted(g2.raw_calls), ["b0", "b2"])

    def test_vol_segment_without_raw_step_still_gets_vol(self):
        # 書目 RAW 前一天手動跑,L1 以後隔天才跑:隔天那段的第一封也要下載
        g = FakeGmail({"r": mail("010201", "gazette-biblio-step1-raw", "14:31", day="30 Sep",
                                 log="RUNDECK:DATA:VOL=5328\n"),
                       "l": mail("010203", "gazette-biblio-step2-l1", "00:30",
                                 log="RUNDECK:DATA:VOL=5328\nx [job_complete] a 1/1 writes=1640 fail=0\n"),
                       "k": mail("010204", "gazette-biblio-step2-l1-check", "00:30")})
        snap = self.make(g).poll()
        self.assertEqual(sorted(g.raw_calls), ["l", "r"])
        twb = next(s for s in snap["status"]["systems"] if s["id"] == "twb-biblio")
        self.assertEqual([p["key"] for p in twb["periods"] if p["last_at"]], ["5328"])

    def test_raw_skipped_when_too_big(self):
        g = FakeGmail({"a": mail("012001", "techreport-pdf-step1-raw", "02:10", "FAILURE",
                                 size=3_500_000, log="x")})
        svc = self.make(g)
        snap = svc.poll()
        self.assertEqual(g.raw_calls, [])
        self.assertEqual(snap["recent"][0]["status"], "fail")
        self.assertFalse(svc.events["a"]["raw"])

    def test_second_poll_skips_known_ids(self):
        g = FakeGmail({"a": mail("012001", "techreport-pdf-step1-raw", "02:10")})
        svc = self.make(g)
        svc.poll()
        svc.poll("manual")
        self.assertEqual(g.meta_calls, ["a"])
        self.assertIn("after:", g.queries[1])
        self.assertEqual(svc.snapshot()["last_poll_kind"], "manual")

    def test_cache_survives_restart(self):
        g = FakeGmail({"a": mail("012001", "techreport-pdf-step1-raw", "02:10")})
        self.make(g).poll()
        svc2 = self.make(FakeGmail({}))
        self.assertIn("a", svc2.events)
        self.assertIsNotNone(svc2.snapshot()["last_poll"])

    def test_traffic_estimate(self):
        g = FakeGmail({"a": mail("012001", "techreport-pdf-step1-raw", "02:10"),
                       "b": mail("012002", "techreport-pdf-step1-raw-check", "02:10"),
                       "c": mail("012003", "techreport-pdf-step2-l1", "02:10", "FAILURE", size=70000)})
        snap = self.make(g).poll()
        self.assertEqual(snap["traffic"]["last_poll_bytes"], 3 * 2048 + 70000)
        self.assertEqual(snap["traffic"]["today_bytes"], 3 * 2048 + 70000)

    def test_auth_error_keeps_events_and_flags(self):
        g = FakeGmail({"a": mail("012001", "techreport-pdf-step1-raw", "02:10")})
        svc = self.make(g)
        svc.poll()
        before = svc.last_poll
        g.fail = AuthError("revoked")
        snap = svc.poll()
        self.assertEqual(snap["auth"], "needs_auth")
        self.assertIn("690授權.bat", snap["last_error"])
        self.assertIn("a", svc.events)
        self.assertEqual(snap["last_poll"], before)

    def test_missing_package(self):
        snap = self.make(ImportError("google")).poll()
        self.assertEqual(snap["auth"], "missing_package")
        self.assertIsNone(snap["last_poll"])

    def test_missing_credentials(self):
        svc = self.make(FakeGmail({}))
        svc.credentials_path = self.tmp / "nope.json"
        self.assertEqual(svc.poll()["auth"], "missing_credentials")

    def test_network_error_sets_last_error(self):
        snap = self.make(FakeGmail({}, fail=GmailError("timeout"))).poll()
        self.assertIn("timeout", snap["last_error"])
        self.assertIsNone(snap["last_poll"])
        self.assertTrue(snap["stale"])

    def test_prunes_old_events(self):
        g = FakeGmail({"old": mail("012001", "techreport-pdf-step1-raw", "02:10", day="01 Aug"),
                       "new": mail("012001", "techreport-pdf-step1-raw", "02:10")})
        svc = self.make(g)
        svc.poll()
        self.assertEqual(set(svc.events), {"new"})

    def test_account_fetched_once_and_cached(self):
        g = FakeGmail({"a": mail("012001", "techreport-pdf-step1-raw", "02:10")})
        g.account_calls = 0

        def account():
            g.account_calls += 1
            return "alicia.huang@innovue.ltd"
        g.account = account
        svc = self.make(g)
        svc.poll()
        svc.poll()
        self.assertEqual(g.account_calls, 1)
        self.assertEqual(svc.snapshot()["gmail_account"], "alicia.huang@innovue.ltd")
        self.assertEqual(self.make(FakeGmail({})).snapshot()["gmail_account"], "alicia.huang@innovue.ltd")

    def test_unrelated_mail_kept_for_dedupe(self):
        g = FakeGmail({"x": ("週報", "Thu, 01 Oct 2026 08:00:00 +0800", 100, None)})
        svc = self.make(g)
        svc.poll()
        svc.poll()
        self.assertEqual(g.meta_calls, ["x"])
        self.assertEqual(svc.snapshot()["recent"], [])

    def test_log_endpoint_fetches_without_storing(self):
        g = FakeGmail({"1a0f996f79c800cf": mail("012001", "techreport-pdf-step1-raw", "02:10", "FAILURE",
                                    size=3_500_000, log=fixture_log("012001"))})
        svc = self.make(g)
        svc.poll()
        self.assertIn("error: GCS", svc.log("1a0f996f79c800cf"))
        with self.assertRaises(KeyError):
            svc.log("../etc")
        with self.assertRaises(KeyError):
            svc.log("ffff")
        self.assertNotIn("GCS 資料夾", (self.tmp / "cache.json").read_text(encoding="utf-8"))


class ClientTest(unittest.TestCase):
    def test_client_update_echoes_fields(self):
        post = FakeWikiPost(wiki_pages())
        c = WikiClient("https://w", "t", post=post)
        p = c.get_page("a/TW全文Full", "zh-tw")
        c.update_content(p, "new")
        v = post.updates[0]
        self.assertEqual(v["content"], "new")
        self.assertEqual(v["tags"], ["a"])
        for k in ("id", "title", "editor", "isPublished", "isPrivate", "locale", "path"):
            self.assertEqual(v[k], p[k])

    def test_client_raises_on_failed_result(self):
        def post(url, headers, body):
            return json.dumps({"data": {"pages": {"update": {
                "responseResult": {"succeeded": False, "message": "nope"}}}}}).encode()
        with self.assertRaises(WikiError):
            WikiClient("https://w", "t", post=post).update_content({"id": 1}, "x")

    def test_bearer_header(self):
        seen = []

        def post(url, headers, body):
            seen.append(headers)
            return b'{"data":{"pages":{"singleByPath":{"id":1,"content":""}}}}'
        WikiClient("https://w/", "T0K", post=post).get_page("p", "zh-tw")
        self.assertEqual(seen[0]["Authorization"], "Bearer T0K")


def daily_events():
    """techreport 10-01 全部成功 + 權利異動 10-01 全部成功 → 每日頁要插一列、填兩組欄。"""
    out = {}
    for code in ["012001", "012002", "012003", "012004", "012005", "012006", "012007", "012008", "012000"]:
        out["t" + code] = mail(code, f"techreport-pdf-{code}", "02:10")
    for code in ["0119%02d" % i for i in range(0, 15)]:
        out["l" + code] = mail(code, f"posted-legal-{code}", "06:10")
    return out


class WikiFlowTest(ServiceCase):
    def test_write_requires_login(self):
        svc = self.make(FakeGmail({}))
        with self.assertRaises(NotLoggedIn):
            svc.wiki_write([])

    def test_login_keeps_jwt_out_of_snapshot_and_cache(self):
        svc = self.make(FakeGmail(daily_events()))
        svc.poll()
        res = svc.wiki_login()
        self.assertTrue(res["ok"])
        snap = json.dumps(svc.snapshot(), ensure_ascii=False)
        self.assertNotIn("JWT1", snap)
        self.assertNotIn("pw", json.dumps(svc.snapshot()["wiki"]))
        self.assertTrue(svc.snapshot()["wiki"]["logged_in"])
        svc.poll()
        self.assertNotIn("JWT1", (self.tmp / "cache.json").read_text(encoding="utf-8"))

    def test_login_failure_message_names_file_not_secret(self):
        svc = self.make(FakeGmail({}), secrets={"wiki_user": "me", "wiki_password": "wrong"})
        res = svc.wiki_login()
        self.assertFalse(res["ok"])
        self.assertIn("secrets.json", res["message"])
        self.assertNotIn("wrong", res["message"])
        self.assertFalse(svc.snapshot()["wiki"]["last_login"]["ok"])

    def test_login_missing_field(self):
        res = self.make(FakeGmail({}), secrets={"wiki_user": "me"}).wiki_login()
        self.assertIn("wiki_password", res["message"])

    def test_dry_run_never_posts_update(self):
        svc = self.make(FakeGmail(daily_events()))
        svc.poll()
        svc.wiki_login()
        res = svc.wiki_write([])
        self.assertTrue(res["dry_run"])
        self.assertEqual(self.wiki_post.updates, [])
        self.assertGreater(res["pages"]["每日執行項目"]["filled"], 0)

    def test_wiki_changed_updates_once_per_page(self):
        svc = self.make(FakeGmail(daily_events()), cfg_patch={"wiki": {"dry_run": False}})
        svc.poll()
        svc.wiki_login()
        res = svc.wiki_write([])
        daily = [u for u in self.wiki_post.updates if u["path"].endswith("每日執行項目")]
        self.assertEqual(len(daily), 1)
        self.assertEqual(res["pages"]["每日執行項目"]["inserted"], 1)
        row = next(l for l in daily[0]["content"].splitlines() if l.startswith("| 2026/10/01"))
        cells = [c.strip() for c in row.strip("|").split("|")]
        self.assertEqual(cells[5:8], ["v", "v", "v"])
        self.assertEqual(cells[9:12], ["v", "v", "v"])
        self.assertEqual(svc.snapshot()["wiki"]["last_write"]["pages"], 1)

    def test_wiki_unchanged_skips_update(self):
        svc = self.make(FakeGmail(daily_events()), cfg_patch={"wiki": {"dry_run": False}})
        svc.poll()
        svc.wiki_login()
        svc.wiki_write([])
        n = len(self.wiki_post.updates)
        svc.wiki_write([])
        self.assertEqual(len(self.wiki_post.updates), n)

    def test_one_page_error_others_continue(self):
        post = FakeWikiPost(wiki_pages(), fail_pages={"TW全文Full"})
        svc = self.make(FakeGmail(daily_events()), wiki=post, cfg_patch={"wiki": {"dry_run": False}})
        svc.poll()
        svc.wiki_login()
        res = svc.wiki_write([])
        self.assertTrue(res["pages"]["TW全文Full"]["errors"])
        self.assertTrue(res["pages"]["每日執行項目"]["updated"])

    def test_expired_jwt_clears_login(self):
        svc = self.make(FakeGmail(daily_events()))
        svc.poll()
        svc.wiki_login()
        self.wiki_post.reject_token = True
        with self.assertRaises(NotLoggedIn):
            svc.wiki_write([])
        w = svc.snapshot()["wiki"]
        self.assertFalse(w["logged_in"])
        self.assertFalse(w["last_login"]["ok"])

    def test_wiki_error_does_not_fail_poll(self):
        svc = self.make(FakeGmail(daily_events()))
        svc.wiki_login()
        self.wiki_post.fail_pages = set(wiki_pages())
        snap = svc.poll()
        self.assertEqual(snap["auth"], "ok")
        self.assertIsNotNone(snap["last_poll"])

    def test_user_edit_overwrites_only_when_expected(self):
        svc = self.make(FakeGmail({}), cfg_patch={"wiki": {"dry_run": False}})
        svc.poll()
        svc.wiki_login()
        res = svc.wiki_write([
            {"page": "TW全文Full", "table": "TWA", "key": "2414", "col": "日期", "expect": "07/11", "value": "07/16"},
            {"page": "TW全文Full", "table": "TWA", "key": "2413", "col": "日期", "expect": "06/30", "value": "07/02"},
        ])
        p = res["pages"]["TW全文Full"]
        self.assertEqual(p["edited"], 1)
        self.assertEqual(len([c for c in p["conflicts"] if c.get("reason") == "頁面上的值已經被改過"]), 1)
        content = self.wiki_post.pages["TW全文Full"]
        self.assertIn("| 07/16 |", content.replace("  ", " ").replace("  ", " "))

    def test_preview_after_login(self):
        svc = self.make(FakeGmail(daily_events()))
        svc.poll()
        svc.wiki_login()
        tables = svc.snapshot()["wiki"]["tables"]
        daily = next(t for t in tables if t["page"] == "每日執行項目")
        row = next(r for r in daily["rows"] if r["key"] == "2026/10/01")
        self.assertTrue(row["new"])
        self.assertEqual(row["cells"][9]["state"], "fill")
        self.assertEqual({t["title"] for t in tables},
                         {"TWA書目Biblio及雜項", "TWB書目Biblio及雜項", "TWA全文Full", "TWB全文Full", "每日執行項目"})

    def test_sync_function_dry(self):
        post = FakeWikiPost(wiki_pages())
        svc = self.make(FakeGmail(daily_events()))
        svc.poll()
        res = sync(WikiClient("https://w", "t", post=post), svc.status(), svc.cfg["wiki"])
        self.assertEqual(post.updates, [])
        self.assertIn("每日執行項目", res["contents"])


if __name__ == "__main__":
    unittest.main()
