"""路由表：端點齊備、分派正確、巢狀路徑不互相吞噬。

不啟動伺服器，只檢查 build_routes() 產出的表本身，以及幾個
「改寫時最容易改壞」的端點行為（狀態碼、回應形狀）。
"""

from unittest.mock import MagicMock, patch

from _util import TempDataCase

from common.webapp import HttpError
from core import routes


def _app(cfg=None):
    return routes.App(cfg=cfg or {"max_count_per_run": 2000},
                      monitor=MagicMock(), runner=MagicMock())


class Req:
    """最小的請求替身：路由表的 lambda 只會碰 body 與 query。"""

    def __init__(self, body=None, query=None):
        self.body = body or {}
        self.query = query or {}


# README「HTTP API 端點」一節列出的完整清單。
# 少一個 = 前端有功能會壞；多一個 = README 沒更新。
EXPECTED = {
    ("GET", "/api/status"),
    ("GET", "/api/job"),
    ("GET", "/api/config"),
    ("GET", "/api/analytics"),
    ("GET", "/api/monthly"),
    ("GET", "/api/monthly/trend"),
    ("GET", "/api/keys"),
    ("GET", "/api/patents"),
    ("POST", "/api/monthly/refresh"),
    ("POST", "/api/run"),
    ("POST", "/api/stop"),
    ("POST", "/api/prefs"),
    ("POST", "/api/keys"),
    ("POST", "/api/clearwarn"),
    ("POST", "/api/speedtest"),
    ("POST", "/api/patents"),
}


class TestRouteTable(TempDataCase):
    def test_every_documented_endpoint_is_registered(self):
        self.assertEqual(set(routes.build_routes(_app())), EXPECTED)

    def test_monthly_and_trend_are_separate_keys(self):
        """v7.2.0 之前的 startswith 長鏈裡，trend 若排錯位置就永遠被吃掉。
        改成精確比對後兩者是獨立的鍵，順序再也不影響。"""
        table = routes.build_routes(_app())
        self.assertIn(("GET", "/api/monthly"), table)
        self.assertIn(("GET", "/api/monthly/trend"), table)
        self.assertIsNot(table[("GET", "/api/monthly")],
                         table[("GET", "/api/monthly/trend")])

    def test_keys_and_patents_differ_by_method(self):
        table = routes.build_routes(_app())
        self.assertIsNot(table[("GET", "/api/keys")], table[("POST", "/api/keys")])
        self.assertIsNot(table[("GET", "/api/patents")], table[("POST", "/api/patents")])

    def test_all_handlers_are_callable(self):
        for key, fn in routes.build_routes(_app()).items():
            self.assertTrue(callable(fn), f"{key} 的 handler 不可呼叫")


class TestJobEndpoints(TempDataCase):
    def test_job_status_delegates_to_runner(self):
        app = _app()
        app.runner.status.return_value = {"state": "idle"}
        table = routes.build_routes(app)
        self.assertEqual(table[("GET", "/api/job")](Req()), {"state": "idle"})

    def test_job_start_conflict_raises_409(self):
        app = _app()
        app.runner.start.return_value = (False, "已經在跑了")
        table = routes.build_routes(app)

        with self.assertRaises(HttpError) as ctx:
            table[("POST", "/api/run")](Req(body={"mode": "count", "count": 5}))
        self.assertEqual(ctx.exception.code, 409)
        self.assertEqual(ctx.exception.payload["message"], "已經在跑了")

    def test_job_start_passes_body_through_in_order(self):
        """start() 是位置參數，順序錯了會把 count 當成 limit_detect 之類。"""
        app = _app()
        app.runner.start.return_value = (True, "已開始")
        table = routes.build_routes(app)
        table[("POST", "/api/run")](Req(body={"mode": "count", "count": 7,
                                              "limit_detect": False, "method": "web"}))
        app.runner.start.assert_called_once_with("count", 7, False, "web")

    def test_job_start_defaults_match_old_handler(self):
        app = _app()
        app.runner.start.return_value = (True, "已開始")
        table = routes.build_routes(app)
        table[("POST", "/api/run")](Req(body={}))
        app.runner.start.assert_called_once_with("count", None, True, "api")

    def test_job_start_no_longer_takes_reserve_mb(self):
        """流量保留底線已於 7.3.0 移除：前端不再送 reserve_mb，
        就算有人硬送一個上來也不該被轉給 runner。"""
        app = _app()
        app.runner.start.return_value = (True, "已開始")
        table = routes.build_routes(app)
        table[("POST", "/api/run")](Req(body={"mode": "count", "count": 5,
                                              "reserve_mb": 999}))
        args, _ = app.runner.start.call_args
        self.assertNotIn(999, args)
        self.assertEqual(args, ("count", 5, True, "api"))

    def test_job_stop_returns_runner_result(self):
        app = _app()
        app.runner.stop.return_value = (True, "已送出停止")
        table = routes.build_routes(app)
        self.assertEqual(table[("POST", "/api/stop")](Req()),
                         {"ok": True, "message": "已送出停止"})

    def test_clearwarn_calls_runner(self):
        app = _app()
        table = routes.build_routes(app)
        self.assertEqual(table[("POST", "/api/clearwarn")](Req()), {"ok": True})
        app.runner.clear_warning.assert_called_once_with()


class TestMonthlyEndpoints(TempDataCase):
    """/api/monthly 與 /api/monthly/trend 失敗時回 200 加空陣列，不是 500。

    前端直接拿 points / months 這些欄位渲染；給它 500 會讓整張達標分析圖
    連同月份下拉一起消失。這是唯一兩個保留自家 try/except 的端點。
    """

    def test_monthly_failure_is_200_with_files_found_false(self):
        mp = MagicMock()
        mp.compute.side_effect = ValueError("匯出檔壞了")
        with patch.object(routes, "monthly_progress", mp):
            got = routes.monthly_now()
        self.assertFalse(got["ok"])
        self.assertFalse(got["files_found"])
        self.assertIn("匯出檔壞了", got["message"])

    def test_trend_failure_is_200_with_empty_arrays(self):
        mp = MagicMock()
        mp.trend.side_effect = ValueError("快照讀不到")
        with patch.object(routes, "monthly_progress", mp):
            got = routes.monthly_trend(None)
        self.assertFalse(got["ok"])
        self.assertEqual(got["points"], [])
        self.assertEqual(got["months"], [])
        self.assertEqual(got["snapshots"], [])

    def test_missing_module_is_also_200_with_empty_arrays(self):
        with patch.object(routes, "monthly_progress", None):
            self.assertEqual(routes.monthly_trend(None)["points"], [])
            self.assertFalse(routes.monthly_now()["files_found"])

    def test_trend_month_param_is_passed_through(self):
        mp = MagicMock()
        mp.trend.return_value = {"month": "2026-08", "points": []}
        table = routes.build_routes(_app())
        with patch.object(routes, "monthly_progress", mp):
            got = table[("GET", "/api/monthly/trend")](Req(query={"month": "2026-08"}))
        mp.trend.assert_called_once_with("2026-08")
        self.assertEqual(got["month"], "2026-08")

    def test_trend_without_month_asks_for_current(self):
        mp = MagicMock()
        mp.trend.return_value = {"month": "2026-09", "points": []}
        table = routes.build_routes(_app())
        with patch.object(routes, "monthly_progress", mp):
            table[("GET", "/api/monthly/trend")](Req())
        mp.trend.assert_called_once_with(None)

    def test_trend_rejects_malformed_month(self):
        """舊版用 regex 從 query string 撈 YYYY-MM，撈不到就當沒給。
        底座改為原樣交出 query 值，格式檢查得自己做。"""
        mp = MagicMock()
        mp.trend.return_value = {"month": "2026-09", "points": []}
        table = routes.build_routes(_app())
        with patch.object(routes, "monthly_progress", mp):
            table[("GET", "/api/monthly/trend")](Req(query={"month": "xyz"}))
        mp.trend.assert_called_once_with(None)

    def test_refresh_failure_is_500_carrying_files_found(self):
        """更新計算失敗要回 500（與讀取不同），但仍帶 files_found 供前端顯示。"""
        mp = MagicMock()
        mp.compute.side_effect = ValueError("算不出來")
        with patch.object(routes, "monthly_progress", mp):
            with self.assertRaises(HttpError) as ctx:
                routes.monthly_refresh()
        self.assertEqual(ctx.exception.code, 500)
        self.assertFalse(ctx.exception.payload["files_found"])

    def test_refresh_records_a_snapshot(self):
        """只有 POST /api/monthly/refresh 才留快照；GET 一律不寫。"""
        mp = MagicMock()
        mp.compute.return_value = {"ok": True, "month": "2026-09"}
        mp.record_snapshot.return_value = {"captured_at": "2026-09-15"}
        mp.trend.return_value = {"points": []}
        with patch.object(routes, "monthly_progress", mp):
            got = routes.monthly_refresh()
        mp.record_snapshot.assert_called_once()
        self.assertEqual(got["snapshot"], {"captured_at": "2026-09-15"})
        self.assertIn("trend", got)

    def test_monthly_get_never_records_a_snapshot(self):
        mp = MagicMock()
        mp.compute.return_value = {"ok": True}
        with patch.object(routes, "monthly_progress", mp):
            routes.monthly_now()
        mp.record_snapshot.assert_not_called()


class TestKeysEndpoints(TempDataCase):
    def _gpss(self, keys):
        g = MagicMock()
        g.load_keys.return_value = list(keys)
        g._mask_code.side_effect = lambda c: (c or "")[:4] + "****"
        return g

    def test_list_returns_masked_codes_only(self):
        """明碼絕不可出網頁。"""
        g = self._gpss([{"id": "A", "label": "甲", "userCode": "ABCD1234SECRET"}])
        g.resolve_current_key.return_value = {"id": "A"}
        with patch.object(routes, "_gpss_module", return_value=g):
            got = routes.keys_list()
        self.assertEqual(got["active"], "A")
        self.assertEqual(got["keys"][0]["masked"], "ABCD****")
        self.assertNotIn("userCode", got["keys"][0])
        self.assertNotIn("SECRET", str(got))

    def test_list_failure_is_200_with_empty_keys(self):
        g = self._gpss([])
        g.load_keys.side_effect = ValueError("api_keys.json 壞了")
        with patch.object(routes, "_gpss_module", return_value=g):
            got = routes.keys_list()
        self.assertFalse(got["ok"])
        self.assertEqual(got["keys"], [])

    def test_add_duplicate_code_raises_409(self):
        g = self._gpss([{"id": "A", "label": "甲", "userCode": "DUP"}])
        with patch.object(routes, "_gpss_module", return_value=g):
            with self.assertRaises(HttpError) as ctx:
                routes.keys_write({"action": "add", "userCode": "DUP"})
        self.assertEqual(ctx.exception.code, 409)
        g.save_keys.assert_not_called()

    def test_add_without_code_raises_400(self):
        g = self._gpss([])
        with patch.object(routes, "_gpss_module", return_value=g):
            with self.assertRaises(HttpError) as ctx:
                routes.keys_write({"action": "add", "userCode": "   "})
        self.assertEqual(ctx.exception.code, 400)
        g.save_keys.assert_not_called()

    def test_add_picks_next_free_letter_id(self):
        g = self._gpss([{"id": "A", "userCode": "X"}, {"id": "B", "userCode": "Y"}])
        with patch.object(routes, "_gpss_module", return_value=g):
            got = routes.keys_write({"action": "add", "label": "丙", "userCode": "Z9"})
        self.assertEqual(got["id"], "C")
        self.assertEqual(got["masked"], "Z9****")
        g.save_keys.assert_called_once()

    def test_switch_to_unknown_id_raises_404(self):
        g = self._gpss([{"id": "A", "userCode": "X"}])
        with patch.object(routes, "_gpss_module", return_value=g):
            with self.assertRaises(HttpError) as ctx:
                routes.keys_write({"id": "Z"})
        self.assertEqual(ctx.exception.code, 404)
        g.write_active_key.assert_not_called()

    def test_switch_writes_active_key(self):
        g = self._gpss([{"id": "A", "label": "甲", "userCode": "ABCD1234"}])
        with patch.object(routes, "_gpss_module", return_value=g):
            got = routes.keys_write({"id": "A"})
        g.write_active_key.assert_called_once()
        self.assertEqual(got["active"], "A")
        self.assertEqual(got["masked"], "ABCD****")

    def test_module_load_failure_raises_500(self):
        with patch.object(routes, "_gpss_module", return_value=None):
            with self.assertRaises(HttpError) as ctx:
                routes.keys_write({"id": "A"})
        self.assertEqual(ctx.exception.code, 500)


class TestPatentsEndpoints(TempDataCase):
    def test_read_counts_only_real_lines(self):
        p = self.tmp / "patents.txt"
        p.write_text("TW123\n# 註解\n\n  \nTW456\n", encoding="utf-8")
        app = _app()
        with patch.object(routes, "gpss_path", return_value=p):
            got = routes.patents_read(app)
        self.assertEqual(got["count"], 2)
        self.assertIn("TW123", got["text"])

    def test_read_missing_file_is_empty_not_an_error(self):
        app = _app()
        with patch.object(routes, "gpss_path", return_value=self.tmp / "nope.txt"):
            got = routes.patents_read(app)
        self.assertEqual(got, {"ok": True, "text": "", "count": 0})

    def test_write_persists_and_counts(self):
        p = self.tmp / "patents.txt"
        app = _app()
        with patch.object(routes, "gpss_path", return_value=p):
            got = routes.patents_write(app, {"text": "TW1\n#x\nTW2\n"})
        self.assertEqual(got["count"], 2)
        self.assertEqual(p.read_text(encoding="utf-8"), "TW1\n#x\nTW2\n")


class TestPrefsEndpoint(TempDataCase):
    def test_threshold_is_clamped(self):
        app = _app()
        with patch.object(routes, "save_prefs", side_effect=lambda p: p):
            hi = routes.prefs_write(app, {"notify_threshold": 999})
            lo = routes.prefs_write(app, {"notify_threshold": -5})
        self.assertEqual(hi["prefs"]["notify_threshold"], 100)
        self.assertEqual(lo["prefs"]["notify_threshold"], 1)

    def test_surge_settings_apply_to_live_monitor(self):
        """改完偏好不必重啟就生效 —— 同時寫檔與套到執行中的 Monitor。"""
        app = _app()
        with patch.object(routes, "save_prefs", side_effect=lambda p: p):
            routes.prefs_write(app, {"surge_enabled": True, "surge_min_mb": 3,
                                     "exclude_lan": True})
        self.assertTrue(app.monitor.surge_enabled)
        self.assertEqual(app.monitor.surge_min_bytes, 3 * 1024 * 1024)
        self.assertTrue(app.monitor.exclude_lan_enabled)

    def test_empty_body_reads_without_writing(self):
        app = _app()
        with patch.object(routes, "save_prefs") as saver, \
             patch.object(routes, "load_prefs", return_value={"notify_enabled": True}):
            got = routes.prefs_write(app, {})
        saver.assert_not_called()
        self.assertEqual(got["prefs"], {"notify_enabled": True})

    def test_garbage_threshold_is_ignored_not_fatal(self):
        app = _app()
        with patch.object(routes, "save_prefs", side_effect=lambda p: p), \
             patch.object(routes, "load_prefs", return_value={}):
            got = routes.prefs_write(app, {"notify_threshold": "abc"})
        self.assertNotIn("notify_threshold", got["prefs"])


class TestStatusPayload(TempDataCase):
    def test_status_merges_health_and_signature_into_snapshot(self):
        app = _app(cfg={"gpss_dir": "program/gpss"})
        app.monitor.snapshot.return_value = {"used_bytes": 1}
        with patch.object(routes, "needs_restart", return_value=True), \
             patch.object(routes, "health_snapshot",
                          return_value={"count": 2, "errors": ["e1", "e2"]}):
            got = routes.status_payload(app)
        self.assertEqual(got["used_bytes"], 1)
        self.assertTrue(got["needs_restart"])
        self.assertEqual(got["server_errors"], 2)
        self.assertEqual(got["recent_errors"], ["e1", "e2"])
        self.assertEqual(len(got["data_sig"]), 16)


class TestRenderIndex(TempDataCase):
    """render_index 收版號當參數，不匯入 traffic_monitor.VERSION —— 否則循環匯入。"""

    def _web(self):
        web = self.tmp / "web"
        (web / "partials").mkdir(parents=True)
        return web

    def test_includes_are_expanded_and_version_substituted(self):
        web = self._web()
        (web / "index.html").write_bytes(
            b"<body><!--#include partials/a.html--><b>v<!--#version--></b></body>")
        (web / "partials" / "a.html").write_bytes(b"<div>AAA</div>")
        with patch.object(routes, "WEB_DIR", web):
            html = routes.render_index("9.9.9")
        self.assertIn(b"<div>AAA</div>", html)
        self.assertIn(b"v9.9.9", html)
        self.assertNotIn(b"#include", html)

    def test_include_path_traversal_is_blocked(self):
        web = self._web()
        (self.tmp / "secret.html").write_bytes(b"LEAK")
        (web / "index.html").write_bytes(b"<!--#include ../secret.html-->")
        with patch.object(routes, "WEB_DIR", web):
            html = routes.render_index("1.0.0")
        self.assertNotIn(b"LEAK", html)

    def test_missing_include_leaves_a_comment_not_a_crash(self):
        web = self._web()
        (web / "index.html").write_bytes(b"<!--#include partials/nope.html-->OK")
        with patch.object(routes, "WEB_DIR", web):
            html = routes.render_index("1.0.0")
        self.assertIn(b"include not found", html)
        self.assertIn(b"OK", html)
