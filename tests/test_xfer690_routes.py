import unittest

import _util  # noqa: F401  (sys.path)
from common.webapp import HttpError, Raw, Request
from core.routes import App, build_routes


class FakeService:
    def __init__(self):
        self.calls = []

    def snapshot(self):
        self.calls.append("snapshot")
        return {"ok": True, "enabled": True}

    def poll(self, kind="auto"):
        self.calls.append(("poll", kind))
        return {"ok": True, "polled": kind}

    def log(self, msg_id):
        self.calls.append(("log", msg_id))
        if msg_id != "abc123":
            raise KeyError(msg_id)
        return "error: x\n"

    def wiki_login(self, jwt):
        self.calls.append(("login", jwt))
        return {"ok": True, "message": ""}

    def wiki_logout(self):
        self.calls.append("logout")
        return {"ok": True}

    def gmail_authorize(self, redirect_uri):
        self.calls.append(("gmail_authorize", redirect_uri))
        return {"ok": True, "url": "https://accounts.google.com/x"}

    def gmail_callback(self, query):
        self.calls.append(("gmail_callback", query))
        return "授權完成"

    def gmail_logout(self):
        self.calls.append("gmail_logout")
        return {"ok": True}

    def wiki_write(self, edits, only=None):
        from xfer690.service import NotLoggedIn
        self.calls.append(("write", edits, only) if only else ("write", edits))
        if edits == "nologin":
            raise NotLoggedIn()
        return {"ok": True}


def req(method, path, query=None, body=None, ctype="application/json"):
    return Request(method, path, query or {}, body or {}, ctype if method == "POST" else "")


class RoutesTest(unittest.TestCase):
    def setUp(self):
        self.svc = FakeService()
        self.routes = build_routes(App({"port": 8765}, None, xfer690=self.svc))

    def call(self, method, path, **kw):
        return self.routes[(method, path)](req(method, path, **kw))

    def test_routes_registered(self):
        for key in [("GET", "/api/xfer690"), ("POST", "/api/xfer690/refresh"),
                    ("GET", "/api/xfer690/log"), ("POST", "/api/xfer690/wiki/login"),
                    ("POST", "/api/xfer690/wiki/logout"), ("POST", "/api/xfer690/wiki/write"),
                    ("POST", "/api/xfer690/gmail/authorize"), ("POST", "/api/xfer690/gmail/logout"),
                    ("GET", "/api/xfer690/gmail/callback")]:
            self.assertIn(key, self.routes)

    def test_status_delegates(self):
        self.assertEqual(self.call("GET", "/api/xfer690"), {"ok": True, "enabled": True})

    def test_refresh_delegates(self):
        self.assertEqual(self.call("POST", "/api/xfer690/refresh")["polled"], "manual")

    def test_log_returns_text(self):
        r = self.call("GET", "/api/xfer690/log", query={"id": "abc123"})
        self.assertIsInstance(r, Raw)
        self.assertTrue(r.content_type.startswith("text/plain"))
        self.assertEqual(r.body, "error: x\n".encode("utf-8"))
        with self.assertRaises(HttpError) as cm:
            self.call("GET", "/api/xfer690/log", query={"id": "zzz"})
        self.assertEqual(cm.exception.code, 404)

    def test_wiki_login_and_write(self):
        self.assertTrue(self.call("POST", "/api/xfer690/wiki/login", body={"jwt": "a.b.c"})["ok"])
        self.assertIn(("login", "a.b.c"), self.svc.calls)
        self.assertTrue(self.call("POST", "/api/xfer690/wiki/logout")["ok"])
        self.assertIn("logout", self.svc.calls)
        only = {"page": "每日執行項目", "table": "", "key": "2026/10/01"}
        self.call("POST", "/api/xfer690/wiki/write", body={"edits": [], "only": only})
        self.assertIn(("write", [], only), self.svc.calls)
        self.call("POST", "/api/xfer690/wiki/write", body={"edits": [{"key": "1"}]})
        self.assertIn(("write", [{"key": "1"}]), self.svc.calls)

    def test_post_requires_json_content_type(self):
        # 別的網站的頁面不經預檢只能送 text/plain / 表單:690 的 POST 一律要 application/json
        for path in ("/api/xfer690/refresh", "/api/xfer690/wiki/login", "/api/xfer690/wiki/logout",
                     "/api/xfer690/wiki/write", "/api/xfer690/gmail/authorize", "/api/xfer690/gmail/logout"):
            for ctype in ("", "text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
                with self.assertRaises(HttpError, msg=f"{path} {ctype}") as cm:
                    self.call("POST", path, body={"edits": [{"key": "1"}]}, ctype=ctype)
                self.assertEqual(cm.exception.code, 415)
        self.assertEqual(self.svc.calls, [])
        self.call("POST", "/api/xfer690/refresh", ctype="application/json; charset=utf-8")
        self.assertIn(("poll", "manual"), self.svc.calls)

    def test_gmail_authorize_and_logout(self):
        self.assertTrue(self.call("POST", "/api/xfer690/gmail/authorize")["ok"])
        self.assertTrue(self.call("POST", "/api/xfer690/gmail/logout")["ok"])
        # Google 同意後導回主程式自己的連接埠(這個伺服器一直開著,不另開暫時伺服器)
        self.assertEqual([c for c in self.svc.calls if "gmail" in str(c)],
                         [("gmail_authorize", "http://127.0.0.1:8765/api/xfer690/gmail/callback"), "gmail_logout"])

    def test_gmail_callback_returns_text(self):
        q = {"state": "s", "code": "c"}
        r = self.call("GET", "/api/xfer690/gmail/callback", query=q)
        self.assertIsInstance(r, Raw)
        self.assertTrue(r.content_type.startswith("text/plain"))
        self.assertEqual(r.body.decode("utf-8"), "授權完成")
        self.assertIn(("gmail_callback", q), self.svc.calls)

    def test_write_without_login_is_409(self):
        with self.assertRaises(HttpError) as cm:
            self.call("POST", "/api/xfer690/wiki/write", body={"edits": "nologin"})
        self.assertEqual(cm.exception.code, 409)
        self.assertEqual(cm.exception.payload, {"ok": False, "error": "not_logged_in"})

    def test_none_service_is_graceful(self):
        routes = build_routes(App({}, None))
        r = routes[("GET", "/api/xfer690")](req("GET", "/api/xfer690"))
        self.assertEqual(r, {"ok": False, "enabled": False, "message": "690 監控模組載入失敗"})
        r = routes[("POST", "/api/xfer690/refresh")](req("POST", "/api/xfer690/refresh"))
        self.assertFalse(r["ok"])
        with self.assertRaises(HttpError):
            routes[("POST", "/api/xfer690/wiki/write")](req("POST", "/api/xfer690/wiki/write"))


if __name__ == "__main__":
    unittest.main()
