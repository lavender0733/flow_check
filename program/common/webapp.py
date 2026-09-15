"""本機 HTTP 伺服器底座。

兩支伺服器（流量監控 8765、年度統計 8766）原本各抄一套一模一樣的管線：
送 JSON、服務靜態檔、路徑穿越防護、快取標頭、靜音 log、吞掉瀏覽器斷線。
全部收斂到這裡；再開第三個小工具時只要寫一張路由表。

路由是 (method, path) -> handler 的精確比對，不是 startswith。舊版用
startswith 有兩個問題：/api/monthly/trend 必須排在 /api/monthly 之前
否則被靜默吃掉，而 /api/statusXYZ 這種亂打的路徑會被誤收。
"""

import json
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

# 只服務這些副檔名。網頁用得到的就這三種；其他一律不給，
# 免得有人把含 userCode 的 .json 放進 web/ 就被整份端出去。
STATIC_TYPES = {
    ".css": "text/css",
    ".js": "application/javascript",
    ".html": "text/html",
}

NO_STORE = "no-store, must-revalidate"


class Request:
    """一個已解析好的請求。handler 只需要看這四個欄位。"""

    __slots__ = ("method", "path", "query", "body")

    def __init__(self, method, path, query, body):
        self.method = method
        self.path = path      # 已去除 query string
        self.query = query    # {str: str}，重複參數取第一個
        self.body = body      # POST 的 JSON；非 JSON 或空 body 為 {}


class Raw:
    """要送出非 JSON 內容時，handler 回傳這個。"""

    __slots__ = ("body", "content_type", "code")

    def __init__(self, body: bytes, content_type: str, code: int = 200):
        self.body = body
        self.content_type = content_type
        self.code = code


def build_handler(routes, static_dir=None, index_renderer=None,
                  on_error=None, quiet=True):
    """造一個 BaseHTTPRequestHandler 子類別。

    routes:         {(method, path): handler}，handler(req) 回傳可 JSON 序列化
                    的物件，或 Raw。
    static_dir:     靜態檔根目錄；None 表示不服務靜態檔。
    index_renderer: 無參數可呼叫物件，回傳 "/" 的 HTML bytes。
                    None 時 "/" 退回讀 static_dir/index.html。
    on_error:       on_error(where, exc)，處理端點例外時呼叫（記錄用）。
    quiet:          True 時不印每個請求的存取記錄。
    """
    static_root = Path(static_dir).resolve() if static_dir else None

    class Handler(BaseHTTPRequestHandler):
        # 沿用舊版的 HTTP/1.0：每個回應都送 Content-Length 並在回應後關閉連線，
        # 本機單使用者的輪詢頁面不需要 keep-alive，少一層連線狀態要管。
        protocol_version = "HTTP/1.0"

        # ── 送出 ────────────────────────────────────────────
        def _send_bytes(self, body: bytes, content_type: str, code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # 不讓瀏覽器快取：否則改了網頁/JS，重整仍可能跑到舊版。
            self.send_header("Cache-Control", NO_STORE)
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, obj, code: int = 200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self._send_bytes(body, "application/json; charset=utf-8", code)

        def _send_status(self, code: int):
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        # ── 解析 ────────────────────────────────────────────
        def _build_request(self, method):
            parsed = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(parsed.query).items() if v}
            body = {}
            if method == "POST":
                try:
                    length = int(self.headers.get("Content-Length", 0) or 0)
                except ValueError:
                    length = 0
                if length > 0:
                    try:
                        body = json.loads(self.rfile.read(length).decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        body = {}
                    if not isinstance(body, dict):
                        body = {}
            return Request(method, unquote(parsed.path), query, body)

        # ── 分派 ────────────────────────────────────────────
        def _dispatch(self, method):
            req = self._build_request(method)
            handler = routes.get((method, req.path))
            if handler is not None:
                try:
                    result = handler(req)
                except Exception as exc:
                    if on_error:
                        on_error(req.path, exc)
                    else:
                        traceback.print_exc()
                    self._send_json({"ok": False, "message": str(exc)}, code=500)
                    return
                if isinstance(result, Raw):
                    self._send_bytes(result.body, result.content_type, result.code)
                else:
                    self._send_json(result)
                return
            if method == "GET":
                self._serve_static(req.path)
                return
            self._send_status(404)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        # ── 靜態檔 ──────────────────────────────────────────
        def _serve_static(self, path):
            if path in ("/", "/index.html") and index_renderer is not None:
                try:
                    body = index_renderer()
                except Exception as exc:
                    if on_error:
                        on_error("index", exc)
                    self._send_json({"ok": False, "message": str(exc)}, code=500)
                    return
                self._send_bytes(body, "text/html; charset=utf-8")
                return

            if static_root is None:
                self._send_status(404)
                return

            rel = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
            target = (static_root / rel).resolve()
            # 路徑穿越防護：解析後必須仍在 static_root 底下。
            try:
                target.relative_to(static_root)
            except ValueError:
                self._send_status(403)
                return
            ctype = STATIC_TYPES.get(target.suffix)
            if ctype is None:
                self._send_status(403)
                return
            try:
                body = target.read_bytes()
            except (OSError, ValueError):
                self._send_status(404)
                return
            self._send_bytes(body, ctype + "; charset=utf-8")

        # ── 雜項 ────────────────────────────────────────────
        def handle(self):
            # 瀏覽器切分頁/重整/關頁時會中途斷線（WinError 10053/10054）。
            # 對這種每幾秒輪詢一次的頁面很常見、也無害 —— 吞掉，別讓主控台
            # 噴一整串 traceback。
            try:
                super().handle()
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass
            except Exception as exc:
                if on_error:
                    on_error("http", exc)

        def log_message(self, fmt, *args):
            if not quiet:
                print(f"[web] {self.address_string()} - {fmt % args}")

    return Handler


def serve(port, routes, static_dir=None, index_renderer=None, on_error=None,
          quiet=True, banner=None, open_browser=True) -> None:
    """綁定連接埠並開始服務，直到 Ctrl+C。

    先綁定再開瀏覽器：連接埠一綁好就開始 listen，此刻開瀏覽器第一個請求
    一定連得上 —— 不會出現「無法連上 127.0.0.1」、要重整才載得出來。
    """
    handler_cls = build_handler(routes, static_dir=static_dir,
                                index_renderer=index_renderer,
                                on_error=on_error, quiet=quiet)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    url = f"http://127.0.0.1:{port}"
    if banner:
        print(banner)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止。")
        httpd.shutdown()
    finally:
        httpd.server_close()
