"""HTTP 底座：路由分派、靜態檔、路徑穿越防護、例外收斂。

不真的開 socket：直接用 build_handler() 造出 Handler 類別，
再以假的 rfile / wfile 驅動它，測試才快且不佔連接埠。
"""

import io
import json

from _util import TempDataCase

from common import webapp


class FakeRequest:
    """驅動 BaseHTTPRequestHandler 所需的最小 socket 替身。

    需要 sendall：BaseHTTPRequestHandler 的 wbufsize 是 0，所以
    StreamRequestHandler.setup() 走 _SocketWriter(self.connection) 這條路，
    寫入直接呼叫 request.sendall() —— 只提供 makefile 的話寫入路徑接不上，
    而且失敗會被 webapp.handle() 的 except 吞掉，表面只看到「沒有輸出」。
    """

    def __init__(self, raw: bytes):
        self._raw = raw
        self.sent = io.BytesIO()

    def makefile(self, mode, *args, **kwargs):
        return io.BytesIO(self._raw) if "r" in mode else self.sent

    def sendall(self, data):
        self.sent.write(data)


def call(handler_cls, method, path, body=None):
    """對 handler 送一個請求，回傳 (status_code, headers: dict, body: bytes)。"""
    payload = b"" if body is None else json.dumps(body).encode("utf-8")
    raw = (f"{method} {path} HTTP/1.1\r\n"
           f"Host: 127.0.0.1\r\n"
           f"Content-Length: {len(payload)}\r\n"
           f"\r\n").encode("utf-8") + payload

    req = FakeRequest(raw)
    handler_cls(req, ("127.0.0.1", 12345), None)
    out = req.sent.getvalue()

    head, _, rest = out.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    code = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    return code, headers, rest


class TestRouting(TempDataCase):
    def make(self, **kw):
        routes = {
            ("GET", "/api/ping"): lambda req: {"ok": True, "who": "ping"},
            ("GET", "/api/monthly"): lambda req: {"which": "monthly"},
            ("GET", "/api/monthly/trend"): lambda req: {"which": "trend",
                                                        "month": req.query.get("month")},
            ("POST", "/api/echo"): lambda req: {"got": req.body},
        }
        return webapp.build_handler(routes, **kw)

    def test_get_route_returns_json(self):
        code, headers, body = call(self.make(), "GET", "/api/ping")
        self.assertEqual(code, 200)
        self.assertIn("application/json", headers["content-type"])
        self.assertEqual(json.loads(body), {"ok": True, "who": "ping"})

    def test_nested_path_does_not_shadow_parent(self):
        """/api/monthly/trend 與 /api/monthly 是兩個獨立的鍵。

        這正是舊版 startswith 長鏈的順序陷阱：trend 若排在 monthly 之後
        就永遠被吃掉，而且不會報錯、只會靜默回錯資料。
        """
        _, _, body = call(self.make(), "GET", "/api/monthly")
        self.assertEqual(json.loads(body)["which"], "monthly")

        _, _, body = call(self.make(), "GET", "/api/monthly/trend")
        self.assertEqual(json.loads(body)["which"], "trend")

    def test_query_string_is_parsed_and_stripped_from_path(self):
        _, _, body = call(self.make(), "GET", "/api/monthly/trend?month=2026-08")
        self.assertEqual(json.loads(body)["month"], "2026-08")

    def test_missing_query_param_is_none(self):
        _, _, body = call(self.make(), "GET", "/api/monthly/trend")
        self.assertIsNone(json.loads(body)["month"])

    def test_prefix_typo_is_404_not_a_silent_match(self):
        """舊版 startswith 會把 /api/pingXYZ 收下；精確比對必須回 404。"""
        code, _, _ = call(self.make(), "GET", "/api/pingXYZ")
        self.assertEqual(code, 404)

    def test_unknown_path_404(self):
        code, _, _ = call(self.make(), "GET", "/api/nope")
        self.assertEqual(code, 404)

    def test_method_matters(self):
        code, _, _ = call(self.make(), "POST", "/api/ping")
        self.assertEqual(code, 404)

    def test_post_body_parsed(self):
        _, _, body = call(self.make(), "POST", "/api/echo", body={"a": 1})
        self.assertEqual(json.loads(body)["got"], {"a": 1})

    def test_post_with_no_body_gets_empty_dict(self):
        _, _, body = call(self.make(), "POST", "/api/echo")
        self.assertEqual(json.loads(body)["got"], {})

    def test_responses_are_not_cached(self):
        """改了 JS/CSS 重整一定要拿到新版，不能被瀏覽器快取擋住。"""
        _, headers, _ = call(self.make(), "GET", "/api/ping")
        self.assertIn("no-store", headers.get("cache-control", ""))


class TestErrorHandling(TempDataCase):
    def test_handler_exception_becomes_500_json(self):
        def boom(req):
            raise ValueError("爆了")

        cls = webapp.build_handler({("GET", "/api/boom"): boom})
        code, headers, body = call(cls, "GET", "/api/boom")
        self.assertEqual(code, 500)
        self.assertIn("application/json", headers["content-type"])
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertIn("爆了", payload["message"])

    def test_on_error_callback_receives_the_exception(self):
        seen = []

        def boom(req):
            raise ValueError("爆了")

        cls = webapp.build_handler({("GET", "/api/boom"): boom},
                                   on_error=lambda where, exc: seen.append((where, exc)))
        call(cls, "GET", "/api/boom")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "/api/boom")
        self.assertIsInstance(seen[0][1], ValueError)

    def test_one_failing_route_does_not_break_the_next_request(self):
        def boom(req):
            raise ValueError("爆了")

        cls = webapp.build_handler({
            ("GET", "/api/boom"): boom,
            ("GET", "/api/ok"): lambda req: {"ok": True},
        })
        call(cls, "GET", "/api/boom")
        code, _, body = call(cls, "GET", "/api/ok")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), {"ok": True})

    def test_http_error_sets_status_and_payload(self):
        def conflict(req):
            raise webapp.HttpError(409, {"ok": False, "message": "已經在跑了"})

        cls = webapp.build_handler({("POST", "/api/run"): conflict})
        code, _, body = call(cls, "POST", "/api/run")
        self.assertEqual(code, 409)
        self.assertEqual(json.loads(body), {"ok": False, "message": "已經在跑了"})

    def test_http_error_from_string_wraps_payload(self):
        def missing(req):
            raise webapp.HttpError(404, "找不到驗證碼 Z")

        cls = webapp.build_handler({("POST", "/api/keys"): missing})
        code, _, body = call(cls, "POST", "/api/keys")
        self.assertEqual(code, 404)
        self.assertEqual(json.loads(body), {"ok": False, "message": "找不到驗證碼 Z"})

    def test_http_error_keeps_extra_payload_fields(self):
        """/api/monthly/refresh 失敗時要帶 files_found 欄位。"""
        def fail(req):
            raise webapp.HttpError(500, {"ok": False, "files_found": False,
                                         "message": "計算失敗"})

        cls = webapp.build_handler({("POST", "/x"): fail})
        code, _, body = call(cls, "POST", "/x")
        self.assertEqual(code, 500)
        self.assertFalse(json.loads(body)["files_found"])

    def test_http_error_does_not_call_on_error(self):
        """HttpError 是預期中的流程控制，不是伺服器故障，不該被記進健康狀態。"""
        seen = []

        def conflict(req):
            raise webapp.HttpError(409, "衝突")

        cls = webapp.build_handler({("POST", "/x"): conflict},
                                   on_error=lambda w, e: seen.append(w))
        call(cls, "POST", "/x")
        self.assertEqual(seen, [])


class TestStaticFiles(TempDataCase):
    def setUp(self):
        super().setUp()
        self.web = self.tmp / "web"
        (self.web / "js").mkdir(parents=True)
        (self.web / "style.css").write_text("body{color:red}", encoding="utf-8")
        (self.web / "js" / "app.js").write_text("console.log(1)", encoding="utf-8")
        (self.web / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
        (self.tmp / "secret.json").write_text('{"userCode":"leak"}', encoding="utf-8")
        self.cls = webapp.build_handler({}, static_dir=self.web)

    def test_serves_css_with_correct_type(self):
        code, headers, body = call(self.cls, "GET", "/style.css")
        self.assertEqual(code, 200)
        self.assertIn("text/css", headers["content-type"])
        self.assertEqual(body, b"body{color:red}")

    def test_serves_nested_js(self):
        code, headers, body = call(self.cls, "GET", "/js/app.js")
        self.assertEqual(code, 200)
        self.assertIn("javascript", headers["content-type"])

    def test_serves_index_at_root(self):
        code, headers, body = call(self.cls, "GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", headers["content-type"])
        self.assertEqual(body, b"<h1>hi</h1>")

    def test_index_html_path_also_works(self):
        code, _, body = call(self.cls, "GET", "/index.html")
        self.assertEqual(code, 200)
        self.assertEqual(body, b"<h1>hi</h1>")

    def test_missing_file_404(self):
        code, _, _ = call(self.cls, "GET", "/js/nope.js")
        self.assertEqual(code, 404)

    def test_path_traversal_is_blocked(self):
        """../ 逃出 web/ 必須被擋 —— 上一層就有含 userCode 的檔案。"""
        for evil in ("/../secret.json",
                     "/js/../../secret.json",
                     "/js/%2e%2e/%2e%2e/secret.json"):
            code, _, body = call(self.cls, "GET", evil)
            self.assertIn(code, (403, 404), f"未擋住：{evil}")
            self.assertNotIn(b"leak", body, f"洩漏內容：{evil}")

    def test_non_whitelisted_extension_rejected(self):
        """只服務 .css/.js/.html；其他副檔名一律不給。"""
        (self.web / "notes.txt").write_text("secret notes", encoding="utf-8")
        code, _, body = call(self.cls, "GET", "/notes.txt")
        self.assertIn(code, (403, 404))
        self.assertNotIn(b"secret notes", body)

    def test_static_files_not_cached(self):
        _, headers, _ = call(self.cls, "GET", "/style.css")
        self.assertIn("no-store", headers.get("cache-control", ""))

    def test_unregistered_api_path_is_404_even_with_static_dir(self):
        """掛了靜態目錄之後，沒對到路由的請求會落到靜態處理。

        /api/statusXYZ 沒有副檔名，不能因此回 403 —— 舊版回的是 404，
        而 403 等於告訴外面「那裡有東西、只是不給」。
        """
        for path in ("/api/statusXYZ", "/api/nope", "/notes.txt"):
            code, _, _ = call(self.cls, "GET", path)
            self.assertEqual(code, 404, f"{path} 應為 404")

    def test_api_route_wins_over_static(self):
        cls = webapp.build_handler({("GET", "/style.css"): lambda req: {"routed": True}},
                                   static_dir=self.web)
        _, _, body = call(cls, "GET", "/style.css")
        self.assertEqual(json.loads(body), {"routed": True})


class TestIndexRenderer(TempDataCase):
    def test_renderer_used_for_root(self):
        web = self.tmp / "web"
        web.mkdir()
        (web / "index.html").write_text("<h1>raw</h1>", encoding="utf-8")
        cls = webapp.build_handler({}, static_dir=web,
                                   index_renderer=lambda: b"<h1>rendered</h1>")
        _, _, body = call(cls, "GET", "/")
        self.assertEqual(body, b"<h1>rendered</h1>")

    def test_renderer_failure_is_500_not_a_crash(self):
        def boom():
            raise FileNotFoundError("index.html not found")

        cls = webapp.build_handler({}, index_renderer=boom)
        code, _, _ = call(cls, "GET", "/")
        self.assertEqual(code, 500)


class TestRaw(TempDataCase):
    def test_raw_response_sent_verbatim(self):
        cls = webapp.build_handler({
            ("GET", "/api/csv"): lambda req: webapp.Raw(b"a,b\n1,2", "text/csv"),
        })
        code, headers, body = call(cls, "GET", "/api/csv")
        self.assertEqual(code, 200)
        self.assertIn("text/csv", headers["content-type"])
        self.assertEqual(body, b"a,b\n1,2")

    def test_raw_custom_status(self):
        cls = webapp.build_handler({
            ("GET", "/api/gone"): lambda req: webapp.Raw(b"", "text/plain", code=410),
        })
        code, _, _ = call(cls, "GET", "/api/gone")
        self.assertEqual(code, 410)
