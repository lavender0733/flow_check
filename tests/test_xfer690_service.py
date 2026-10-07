import json
import threading
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from _util import TempDataCase
from core.paths import XFER690_CONFIG_PATH
from xfer690.gmail_client import AuthError, GmailError
from xfer690.parser import TPE
from xfer690.service import NotLoggedIn, Xfer690Service
from xfer690.wiki import WikiAuthError, WikiClient, WikiError, sync
from xfer690_mail import FIX, fixture_log, make_eml, subject

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=TPE)
REAL_CFG = json.loads(XFER690_CONFIG_PATH.read_text(encoding="utf-8"))
TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJpZCI6N30.c2ln"      # 長得像 JWT 的假 token


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
        self.limits = getattr(self, "limits", []) + [limit]
        return list(self.messages)[:limit]

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
    """假的 Wiki.js GraphQL 伺服器:頁面放記憶體,記錄每次 update。

    只認 self.valid 裡的 token;renew_to 有值時,下一個請求回 new-jwt 標頭(Wiki.js 的續期)。"""

    def __init__(self, pages, fail_pages=(), jwt=None):
        self.pages = dict(pages)        # path 結尾頁名 → content
        self.fail_pages = set(fail_pages)
        self.updates = []
        self.valid = {jwt or TOKEN}
        self.renew_to = None
        self.reject_token = False

    def __call__(self, url, headers, body):
        req = json.loads(body)
        q, v = req["query"], req["variables"]
        tok = headers.get("Authorization", "")[len("Bearer "):]
        if self.reject_token or tok not in self.valid:
            if "profile" in q:      # Wiki.js 把無效的 token 當訪客:只有要身分的查詢才報錯
                return json.dumps({"errors": [{"message": "You must be authenticated to access this resource."}],
                                   "data": {"users": {"profile": None}}}).encode()
            return json.dumps({"errors": [{"message": "Forbidden"}]}).encode()
        out_headers = {}
        if self.renew_to:
            self.valid = {self.renew_to}
            out_headers["new-jwt"] = self.renew_to
            self.renew_to = None
        if "profile" in q:
            data = {"data": {"users": {"profile": {"name": "Alicia", "email": "alicia@innovue.ltd"}}}}
        elif "singleByPath" in q:
            name = v["p"].rsplit("/", 1)[-1]
            if name in self.fail_pages:
                return json.dumps({"errors": [{"message": "boom"}]}).encode()
            data = {"data": {"pages": {"singleByPath": {
                "id": hash(name) % 9999, "content": self.pages[name], "title": name, "description": "",
                "editor": "markdown", "isPublished": True, "isPrivate": False, "locale": "zh-tw",
                "path": v["p"], "tags": [{"tag": "a"}], "scriptCss": "", "scriptJs": ""}}}}
        elif "update(" in q:
            self.updates.append(v)
            self.pages[v["path"].rsplit("/", 1)[-1]] = v["content"]
            data = {"data": {"pages": {"update": {"responseResult": {"succeeded": True}}}}}
        else:
            raise AssertionError(q)
        return json.dumps(data).encode(), out_headers


def wiki_pages():
    return {"TW書目Biblio及雜項": (FIX / "wiki_biblio.md").read_bytes().decode("utf-8"),
            "TW全文Full": (FIX / "wiki_full.md").read_bytes().decode("utf-8"),
            "每日執行項目": (FIX / "wiki_daily.md").read_bytes().decode("utf-8"),
            "TW英譯En": (FIX / "wiki_eng.md").read_bytes().decode("utf-8")}


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
        sec = self.write_json("secrets.json", secrets if secrets is not None else {})
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

    def test_raw_for_eng_span(self):
        # 英譯的 KM 列鍵(期數區間)在 log 的 FILE_I:成功的那段也要抓第一封子步驟
        log = "RUNDECK:DATA:FILE_I=TW__I_20260901_5325_to_20260921_5327_complete\nRUNDECK:DATA:TAG=20261005\n"
        g = FakeGmail({f"e{j}": mail(c, f"eng-{c}", "01:00", day="05 Oct", log=log if c == "011301" else None)
                       for j, c in enumerate(["011301", "011302", "011303", "011304", "011305", "011306", "011300"])})
        snap = self.make(g, now=datetime(2026, 10, 7, 9, 0, tzinfo=TPE)).poll()
        self.assertEqual(g.raw_calls, ["e0"])
        eng = next(s for s in snap["status"]["systems"] if s["id"] == "eng")
        self.assertEqual(eng["periods"][0]["state"], "ok")

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

    def test_list_limit_from_config(self):
        g = FakeGmail({})
        self.make(g).poll()
        self.assertEqual(g.limits, [5000])

    def test_truncated_listing_does_not_advance_last_poll(self):
        # 信多到超過上限:最舊的那些沒列到。last_poll 若照常前進,下次 after: 就再也不會涵蓋它們
        g = FakeGmail({"a": mail("012001", "techreport-pdf-step1-raw", "02:10"),
                       "b": mail("012002", "techreport-pdf-step1-raw-check", "02:10"),
                       "c": mail("012000", "techreport-pdf-auto", "02:11")})
        svc = self.make(g, cfg_patch={"max_list": 2})
        snap = svc.poll()
        self.assertEqual(sorted(svc.events), ["a", "b"])     # 列到的照樣存
        self.assertIsNone(snap["last_poll"])
        self.assertIn("max_list", snap["last_error"])
        self.assertEqual(snap["auth"], "ok")
        svc.poll()
        self.assertIn("newer_than:45d", g.queries[1])        # 下次還是從頭列

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
        self.assertIn("重新授權", snap["last_error"])
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


class _GatewayHandler(BaseHTTPRequestHandler):
    """模仿 Cloudflare:User-Agent 是 Python-urllib 就回 403「error code: 1010」(純文字,不是 JSON)。"""

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).seen_ua.append(self.headers.get("User-Agent", ""))
        if "python-urllib" in self.headers.get("User-Agent", "").lower() or type(self).block_all:
            body, code, ctype = b"error code: 1010", 403, "text/plain"
        else:
            body, code, ctype = b'{"data":{"users":{"profile":{"name":"A","email":"a@b"}}}}', 200, "application/json"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class GatewayTest(unittest.TestCase):
    """不開 VPN 時 KM 走 Cloudflare:擋掉 Python-urllib,而且擋下來的 403 不是 jwt 失效。"""

    def setUp(self):
        _GatewayHandler.seen_ua, _GatewayHandler.block_all = [], False
        self.httpd = HTTPServer(("127.0.0.1", 0), _GatewayHandler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_real_post_sends_own_user_agent(self):
        self.assertEqual(WikiClient(self.url, TOKEN).whoami(), {"name": "A", "email": "a@b"})
        self.assertTrue(_GatewayHandler.seen_ua[0].startswith("flow-check-xfer690"), _GatewayHandler.seen_ua)

    def test_gateway_block_is_not_auth_error(self):
        _GatewayHandler.block_all = True
        with self.assertRaises(WikiError) as cm:
            WikiClient(self.url, TOKEN).whoami()
        self.assertNotIsInstance(cm.exception, WikiAuthError)
        self.assertIn("1010", str(cm.exception))


class ClientTest(unittest.TestCase):
    def test_client_update_echoes_fields(self):
        post = FakeWikiPost(wiki_pages())
        c = WikiClient("https://w", TOKEN, post=post)
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
            WikiClient("https://w", TOKEN, post=post).update_content({"id": 1}, "x")

    def test_whoami_guest_is_auth_error(self):
        post = FakeWikiPost(wiki_pages())
        with self.assertRaises(WikiAuthError):
            WikiClient("https://w", "nope", post=post).whoami()
        self.assertEqual(WikiClient("https://w", TOKEN, post=post).whoami(),
                         {"name": "Alicia", "email": "alicia@innovue.ltd"})

    def test_client_picks_up_new_jwt(self):
        post = FakeWikiPost(wiki_pages())
        post.renew_to = "eyJh.eyJw.bmV3"
        c = WikiClient("https://w", TOKEN, post=post)
        c.get_page("a/TW全文Full", "zh-tw")
        self.assertEqual(c.token, "eyJh.eyJw.bmV3")

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


class FakeFlow:
    """假的 google_auth_oauthlib Flow:記錄 redirect_uri 和換 token 用的 code。"""

    def __init__(self, redirect_uri, fail=None):
        self.redirect_uri = redirect_uri
        self.fail = fail
        self.codes = []
        self.credentials = self

    def authorization_url(self, **kw):
        return f"https://accounts.google.com/o/oauth2/auth?state=st1&redirect_uri={self.redirect_uri}", "st1"

    def fetch_token(self, code):
        if self.fail:
            raise self.fail
        self.codes.append(code)

    def to_json(self):
        return '{"token": "t", "refresh_token": "r"}'


class GmailAuthTest(ServiceCase):
    """信件與連線的 Gmail 卡片:沒授權時「授權」,已授權時「登出」。"""

    CB = "http://127.0.0.1:8765/api/xfer690/gmail/callback"

    def make_auth(self, gmail, fail=None):
        svc = self.make(gmail)
        svc.token_path = self.tmp / "gmail_token.json"
        self.flows = []

        def factory(cred_path, redirect_uri):
            f = FakeFlow(redirect_uri, fail)
            self.flows.append(f)
            return f
        svc.flow_factory = factory
        return svc

    def wait_poll(self, svc):
        for t in threading.enumerate():
            if t is not threading.current_thread() and t.daemon:
                t.join(5)

    def test_authorize_returns_url_then_callback_saves_token(self):
        g = FakeGmail({"a": mail("012001", "techreport-pdf-step1-raw", "02:10")})
        svc = self.make_auth(g)
        res = svc.gmail_authorize(self.CB)          # 不等使用者:馬上回網址給網頁開
        self.assertTrue(res["ok"])
        self.assertIn("accounts.google.com", res["url"])
        self.assertEqual(self.flows[0].redirect_uri, self.CB)
        self.assertTrue(svc.snapshot()["gmail_authorizing"])
        msg = svc.gmail_callback({"state": "st1", "code": "c0de"})
        self.assertIn("授權完成", msg)
        self.assertEqual(self.flows[0].codes, ["c0de"])
        self.assertTrue(svc.token_path.exists())
        self.wait_poll(svc)
        snap = svc.snapshot()
        self.assertFalse(snap["gmail_authorizing"])
        self.assertEqual(snap["auth"], "ok")        # 授權完馬上抓一次信
        self.assertTrue(snap["gmail_authorized"])

    def test_callback_with_wrong_state_ignored(self):
        svc = self.make_auth(FakeGmail({}))
        svc.gmail_authorize(self.CB)
        self.assertIn("失效", svc.gmail_callback({"state": "other", "code": "x"}))
        self.assertIn("失效", svc.gmail_callback({"code": "x"}))
        self.assertEqual(self.flows[0].codes, [])
        self.assertFalse(svc.token_path.exists())
        self.assertTrue(svc.snapshot()["gmail_authorizing"])     # 還在等真正的回呼

    def test_callback_without_pending_authorize(self):
        svc = self.make_auth(FakeGmail({}))
        self.assertIn("失效", svc.gmail_callback({"state": "st1", "code": "x"}))
        self.assertFalse(svc.token_path.exists())

    def test_user_denied(self):
        svc = self.make_auth(FakeGmail({}))
        svc.gmail_authorize(self.CB)
        msg = svc.gmail_callback({"state": "st1", "error": "access_denied"})
        self.assertIn("沒有完成", msg)
        snap = svc.snapshot()
        self.assertFalse(snap["gmail_authorizing"])
        self.assertIn("access_denied", snap["last_error"])
        self.assertFalse(svc.token_path.exists())

    def test_token_exchange_failure_reported(self):
        svc = self.make_auth(FakeGmail({}), fail=RuntimeError("invalid_grant"))
        svc.gmail_authorize(self.CB)
        self.assertIn("沒有完成", svc.gmail_callback({"state": "st1", "code": "c"}))
        snap = svc.snapshot()
        self.assertFalse(snap["gmail_authorizing"])
        self.assertIn("授權沒有完成", snap["last_error"])

    def test_authorizing_expires(self):
        svc = self.make_auth(FakeGmail({}))
        svc.gmail_authorize(self.CB)
        self.clock[0] = NOW + timedelta(minutes=11)
        self.assertFalse(svc.snapshot()["gmail_authorizing"])

    def test_authorize_again_replaces_pending(self):
        svc = self.make_auth(FakeGmail({}))
        svc.gmail_authorize(self.CB)
        svc.gmail_authorize(self.CB)                # 分頁被關掉,再按一次
        self.assertEqual(len(self.flows), 2)
        svc.gmail_callback({"state": "st1", "code": "c"})
        self.assertEqual(self.flows[0].codes, [])
        self.assertEqual(self.flows[1].codes, ["c"])

    def test_authorize_without_credentials_file(self):
        svc = self.make_auth(FakeGmail({}))
        svc.credentials_path.unlink()
        res = svc.gmail_authorize(self.CB)
        self.assertFalse(res["ok"])
        self.assertIn(svc.credentials_path.name, res["message"])
        self.assertEqual(self.flows, [])

    def test_logout_removes_token(self):
        svc = self.make_auth(FakeGmail({}))
        svc.token_path.write_text('{"token": "t"}', encoding="utf-8")
        svc.poll()
        svc.account = "a@b"
        svc.gmail_logout()
        self.assertFalse(svc.token_path.exists())
        snap = svc.snapshot()
        self.assertEqual(snap["auth"], "needs_auth")
        self.assertIsNone(snap["gmail_account"])
        self.assertFalse(snap["gmail_authorized"])

    def test_poll_after_logout_stays_logged_out(self):
        # 登出後自動抓信:真的 GmailClient 找不到 token → AuthError → 維持未授權,不會又變成已授權
        from xfer690.gmail_client import GmailClient
        svc = self.make_auth(FakeGmail({}))
        svc.gmail_factory = lambda: GmailClient(svc.token_path)
        svc.gmail_logout()
        snap = svc.poll()
        self.assertEqual(snap["auth"], "needs_auth")
        self.assertFalse(snap["gmail_authorized"])

    def test_authorized_flag(self):
        svc = self.make_auth(FakeGmail({}))
        self.assertFalse(svc.snapshot()["gmail_authorized"])       # 沒有 token
        svc.token_path.write_text('{"token": "t"}', encoding="utf-8")
        svc.poll()
        self.assertTrue(svc.snapshot()["gmail_authorized"])
        svc.auth = "needs_auth"                                      # token 在、但被撤銷
        self.assertFalse(svc.snapshot()["gmail_authorized"])


class WikiFlowTest(ServiceCase):
    def test_write_requires_login(self):
        svc = self.make(FakeGmail({}))
        with self.assertRaises(NotLoggedIn):
            svc.wiki_write([])

    def test_login_keeps_jwt_out_of_snapshot_and_cache(self):
        svc = self.make(FakeGmail(daily_events()))
        svc.poll()
        res = svc.wiki_login(TOKEN)
        self.assertTrue(res["ok"])
        self.assertNotIn(TOKEN, json.dumps(res, ensure_ascii=False))
        snap = json.dumps(svc.snapshot(), ensure_ascii=False)
        self.assertNotIn(TOKEN, snap)
        self.assertTrue(svc.snapshot()["wiki"]["logged_in"])
        self.assertEqual(svc.snapshot()["wiki"]["account"], "Alicia(alicia@innovue.ltd)")
        svc.poll()
        self.assertNotIn(TOKEN, (self.tmp / "cache.json").read_text(encoding="utf-8"))

    def test_login_saves_jwt_to_secrets_and_survives_restart(self):
        svc = self.make(FakeGmail(daily_events()), secrets={"note": "keep"})
        svc.wiki_login(TOKEN)
        sec = json.loads(svc.secrets_path.read_text(encoding="utf-8"))
        self.assertEqual(sec["wiki_jwt"], TOKEN)
        self.assertEqual(sec["note"], "keep")       # 其他欄位不動
        svc2 = Xfer690Service(config_path=svc.config_path, cache_path=svc.cache_path,
                              secrets_path=svc.secrets_path, credentials_path=svc.credentials_path,
                              gmail_factory=svc.gmail_factory, wiki_factory=svc.wiki_factory, now=svc.now)
        self.assertTrue(svc2.snapshot()["wiki"]["logged_in"])
        svc2.poll()
        self.assertTrue(svc2.snapshot()["wiki"]["tables"])

    def test_login_accepts_cookie_paste_forms(self):
        for pasted in (f"  {TOKEN}\n", f"jwt={TOKEN}", f"Bearer {TOKEN}"):
            svc = self.make(FakeGmail({}))
            self.assertTrue(svc.wiki_login(pasted)["ok"], pasted)

    def test_login_rejects_bad_shape_without_network(self):
        svc = self.make(FakeGmail({}))
        for bad in ("", None, "abc", "a.b", "<script>.x.y", 123):
            res = svc.wiki_login(bad)
            self.assertFalse(res["ok"], bad)
        self.assertFalse(svc.snapshot()["wiki"]["logged_in"])

    def test_login_rejects_token_wiki_does_not_accept(self):
        # Wiki.js 把無效 token 當訪客,不會報 401:要用「我是誰」的查詢確認
        svc = self.make(FakeGmail({}))
        other = "eyJhbGciOiJSUzI1NiJ9.eyJpZCI6OX0.eHh4"
        res = svc.wiki_login(other)
        self.assertFalse(res["ok"])
        self.assertNotIn(other, res["message"])
        self.assertIn("過期", res["message"])
        self.assertFalse(svc.snapshot()["wiki"]["logged_in"])
        self.assertNotIn(other, svc.secrets_path.read_text(encoding="utf-8"))

    def test_renewed_jwt_written_back(self):
        svc = self.make(FakeGmail(daily_events()))
        svc.wiki_login(TOKEN)
        new = "eyJhbGciOiJSUzI1NiJ9.eyJpZCI6N30.bmV3"
        self.wiki_post.renew_to = new
        svc.poll()                                  # 預覽讀頁面時拿到 new-jwt
        self.assertEqual(json.loads(svc.secrets_path.read_text(encoding="utf-8"))["wiki_jwt"], new)
        svc.wiki_write([])                          # 舊 token 已經不收,要用新的
        self.assertTrue(svc.snapshot()["wiki"]["logged_in"])

    def test_logout_clears_jwt_everywhere(self):
        svc = self.make(FakeGmail({}), secrets={"note": "keep"})
        svc.wiki_login(TOKEN)
        svc.wiki_logout()
        self.assertFalse(svc.snapshot()["wiki"]["logged_in"])
        self.assertEqual(json.loads(svc.secrets_path.read_text(encoding="utf-8")), {"note": "keep"})
        with self.assertRaises(NotLoggedIn):
            svc.wiki_write([])

    def test_login_rereads_config(self):
        # 使用者改了 xfer690.json 的 wiki.enabled 之後直接按登入:不用等下一次抓信
        svc = self.make(FakeGmail({}), cfg_patch={"wiki": {"enabled": False}})
        svc.poll()
        self.assertFalse(svc.wiki_login(TOKEN)["ok"])
        cfg = json.loads(svc.config_path.read_text(encoding="utf-8"))
        cfg["wiki"]["enabled"] = True
        svc.config_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self.assertTrue(svc.wiki_login(TOKEN)["ok"])

    def test_dry_run_never_posts_update(self):
        svc = self.make(FakeGmail(daily_events()), cfg_patch={"wiki": {"dry_run": True}})
        svc.poll()
        svc.wiki_login(TOKEN)
        res = svc.wiki_write([])
        self.assertTrue(res["dry_run"])
        self.assertEqual(self.wiki_post.updates, [])
        self.assertGreater(res["pages"]["每日執行項目"]["filled"], 0)

    def test_wiki_changed_updates_once_per_page(self):
        svc = self.make(FakeGmail(daily_events()), cfg_patch={"wiki": {"dry_run": False}})
        svc.poll()
        svc.wiki_login(TOKEN)
        res = svc.wiki_write([])
        daily = [u for u in self.wiki_post.updates if u["path"].endswith("每日執行項目")]
        self.assertEqual(len(daily), 1)
        self.assertEqual(res["pages"]["每日執行項目"]["inserted"], 1)
        row = next(l for l in daily[0]["content"].splitlines() if l.startswith("| 2026/10/01"))
        cells = [c.strip() for c in row.strip("|").split("|")]
        self.assertEqual(cells[5:8], ["v", "v", "v"])
        self.assertEqual(cells[9:12], ["v", "v", "v"])
        self.assertEqual(svc.snapshot()["wiki"]["last_write"]["pages"], 1)

    def test_write_only_one_row(self):
        # 第一次正式寫入先試一列:只動那一頁那一列,其他頁、其他列、其他格都不碰
        evs = daily_events()
        evs.update({"s" + c: mail(c, f"posted-status-{c}", "07:10", day="30 Sep")
                    for c in ["0117%02d" % i for i in range(0, 17)]})
        svc = self.make(FakeGmail(evs), cfg_patch={"wiki": {"dry_run": False}})
        svc.poll()
        svc.wiki_login(TOKEN)
        before = dict(self.wiki_post.pages)
        res = svc.wiki_write([], only={"page": "每日執行項目", "table": "", "key": "2026/10/01"})
        self.assertTrue(res["ok"])
        self.assertEqual([u["path"].rsplit("/", 1)[-1] for u in self.wiki_post.updates], ["每日執行項目"])
        old = before["每日執行項目"].splitlines(keepends=True)
        new = self.wiki_post.pages["每日執行項目"].splitlines(keepends=True)
        added = [l for l in new if l not in old]
        self.assertEqual(len(new), len(old) + 1)                 # 只多一列
        self.assertEqual(len(added), 1)
        self.assertTrue(added[0].startswith("| 2026/10/01"))
        self.assertNotIn("2026/09/30", self.wiki_post.pages["每日執行項目"])   # 別的新列沒插
        p = res["pages"]["每日執行項目"]
        self.assertEqual(p["inserted"], 1)
        self.assertEqual(res["last_write"]["only"], "每日執行項目 2026/10/01")
        for page in ("TW全文Full", "TW書目Biblio及雜項"):
            self.assertEqual(self.wiki_post.pages[page], before[page])

    def test_write_only_sends_that_rows_edits(self):
        # 「只寫這一列」:那一列的手改一起送(新增的列也能改),別列的手改不送
        svc = self.make(FakeGmail(daily_events()), cfg_patch={"wiki": {"dry_run": False}})
        svc.poll()
        svc.wiki_login(TOKEN)
        res = svc.wiki_write([
            {"page": "每日執行項目", "table": "", "key": "2026/10/01", "col": "RAW", "system": "techreport",
             "expect": "", "value": "x"},
            {"page": "TW全文Full", "table": "TWA", "key": "2414", "col": "日期", "expect": "07/11", "value": "07/16"}],
            only={"page": "每日執行項目", "table": "", "key": "2026/10/01"})
        self.assertEqual(res["pages"]["每日執行項目"]["edited"], 1)
        row = next(l for l in self.wiki_post.pages["每日執行項目"].splitlines() if l.startswith("| 2026/10/01"))
        self.assertEqual([c.strip() for c in row.strip().strip("|").split("|")][9], "x")
        self.assertNotIn("07/16", self.wiki_post.pages["TW全文Full"])

    def test_write_only_ignores_edits_and_bad_target(self):
        svc = self.make(FakeGmail(daily_events()), cfg_patch={"wiki": {"dry_run": False}})
        svc.poll()
        svc.wiki_login(TOKEN)
        res = svc.wiki_write(
            [{"page": "TW全文Full", "table": "TWA", "key": "2414", "col": "日期", "expect": "07/11", "value": "07/16"}],
            only={"page": "每日執行項目", "table": "", "key": "2026/10/01"})
        self.assertEqual(res["pages"]["每日執行項目"]["edited"], 0)
        self.assertNotIn("07/16", self.wiki_post.pages["TW全文Full"])
        n = len(self.wiki_post.updates)
        for bad in ({"page": "不存在", "table": "", "key": "x"}, {"page": "每日執行項目"}, "x"):
            r = svc.wiki_write([], only=bad)
            self.assertFalse(r["ok"], bad)
        self.assertEqual(len(self.wiki_post.updates), n)

    def test_wiki_unchanged_skips_update(self):
        svc = self.make(FakeGmail(daily_events()), cfg_patch={"wiki": {"dry_run": False}})
        svc.poll()
        svc.wiki_login(TOKEN)
        svc.wiki_write([])
        n = len(self.wiki_post.updates)
        svc.wiki_write([])
        self.assertEqual(len(self.wiki_post.updates), n)

    def test_one_page_error_others_continue(self):
        post = FakeWikiPost(wiki_pages(), fail_pages={"TW全文Full"})
        svc = self.make(FakeGmail(daily_events()), wiki=post, cfg_patch={"wiki": {"dry_run": False}})
        svc.poll()
        svc.wiki_login(TOKEN)
        res = svc.wiki_write([])
        self.assertTrue(res["pages"]["TW全文Full"]["errors"])
        self.assertTrue(res["pages"]["每日執行項目"]["updated"])

    def test_expired_jwt_clears_login(self):
        svc = self.make(FakeGmail(daily_events()))
        svc.poll()
        svc.wiki_login(TOKEN)
        self.wiki_post.reject_token = True
        with self.assertRaises(NotLoggedIn):
            svc.wiki_write([])
        w = svc.snapshot()["wiki"]
        self.assertFalse(w["logged_in"])
        self.assertFalse(w["last_login"]["ok"])
        # 失效的 token 也從檔案拿掉,重開後不會再拿它去試
        self.assertNotIn("wiki_jwt", json.loads(svc.secrets_path.read_text(encoding="utf-8")))

    def test_gateway_block_keeps_login(self):
        # 不開 VPN 時被 Cloudflare 擋(HTTP 403、不是 JSON):是網路問題,不能清掉 jwt、不能登出
        svc = self.make(FakeGmail(daily_events()), cfg_patch={"wiki": {"dry_run": False}})
        svc.poll()
        svc.wiki_login(TOKEN)

        def blocked(url, headers, body):
            raise WikiError("KM 前面的閘道擋下連線(HTTP 403:error code: 1010)")
        svc.wiki_factory = lambda url, token=None: WikiClient(url, token, post=blocked)
        res = svc.wiki_write([])                    # 每頁各自失敗(一頁失敗不影響其他頁的既有約定)
        self.assertTrue(all("1010" in " ".join(p["errors"]) for p in res["pages"].values()))
        self.assertFalse(any(p["updated"] for p in res["pages"].values()))
        svc.wiki_factory = lambda url, token=None: WikiClient(url, token, post=blocked)
        svc.poll()                                  # 預覽讀不到頁面也一樣
        w = svc.snapshot()["wiki"]
        self.assertTrue(w["logged_in"])
        self.assertEqual(json.loads(svc.secrets_path.read_text(encoding="utf-8"))["wiki_jwt"], TOKEN)
        self.assertIn("1010", w["last_error"])
        # 登入時被擋:回網路錯誤,也不說成 jwt 不對
        res = svc.wiki_login(TOKEN)
        self.assertFalse(res["ok"])
        self.assertNotIn("不認得", res["message"])
        self.assertIn("1010", res["message"])

    def test_wiki_error_does_not_fail_poll(self):
        svc = self.make(FakeGmail(daily_events()))
        svc.wiki_login(TOKEN)
        self.wiki_post.fail_pages = set(wiki_pages())
        snap = svc.poll()
        self.assertEqual(snap["auth"], "ok")
        self.assertIsNotNone(snap["last_poll"])

    def test_user_edit_overwrites_only_when_expected(self):
        svc = self.make(FakeGmail({}), cfg_patch={"wiki": {"dry_run": False}})
        svc.poll()
        svc.wiki_login(TOKEN)
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
        svc.wiki_login(TOKEN)
        tables = svc.snapshot()["wiki"]["tables"]
        daily = next(t for t in tables if t["page"] == "每日執行項目")
        row = next(r for r in daily["rows"] if r["key"] == "2026/10/01")
        self.assertTrue(row["new"])
        self.assertEqual(row["cells"][9]["state"], "fill")
        self.assertEqual({t["title"] for t in tables},
                         {"TWA書目Biblio及雜項", "TWB書目Biblio及雜項", "TWA全文Full", "TWB全文Full", "每日執行項目",
                          "TWA英譯En", "TWB英譯En"})

    def test_sync_function_dry(self):
        post = FakeWikiPost(wiki_pages())
        svc = self.make(FakeGmail(daily_events()), cfg_patch={"wiki": {"dry_run": True}})
        svc.poll()
        res = sync(WikiClient("https://w", TOKEN, post=post), svc.status(), svc.cfg["wiki"])
        self.assertEqual(post.updates, [])
        self.assertIn("每日執行項目", res["contents"])


if __name__ == "__main__":
    unittest.main()
