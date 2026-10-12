"""HTTP 端點。

每個端點一個函式，最下方以一張表註冊。新增端點 = 寫一個函式 + 加一行註冊。

路由由 common.webapp 以 (method, path) 精確比對分派，所以註冊順序無關緊要。

端點函式只寫正常路徑：例外由底座統一攔截、統一記進 health.record_error。
要回非 200 狀態碼時拋 webapp.HttpError。
"""

import re

from common.webapp import HttpError, Raw
from core.health import health_snapshot, needs_restart
from core.paths import MACHINE_ID, WEB_DIR
from core.sampler import run_speedtest, traffic_log_dates
from core.storage import load_prefs, machine_label, save_prefs


class App:
    """持有伺服器執行期的物件，讓端點函式能取用。

    取代舊版把 monitor / config 掛成 Handler 類別屬性的做法 ——
    那是全域可變狀態，且讓 Handler 無法被獨立測試。
    """

    __slots__ = ("cfg", "monitor", "xfer690")

    def __init__(self, cfg, monitor, xfer690=None):
        self.cfg = cfg
        self.monitor = monitor
        # 690 資轉監控(program/xfer690)。TIPO flow check 沒有這個套件、或載入失敗時是 None,
        # 那幾個端點回「模組載入失敗」,流量監控照常。
        self.xfer690 = xfer690


# ============================================================
# 首頁組版
# ============================================================
# index.html 是外殼:頁籤按鈕列由前端依註冊表生成,面板與 js 由這裡掃目錄注入。
# 送頁時即時掃、即時讀 —— 丟了新檔重整就看得到,不必重啟。
#
# 掃的是 web/partials/*.html,不是 web/js/*.js:每個頁籤模組都由「一個面板 + 一支 js」
# 成對組成,以面板為準才擋得住不該載入的 js。這個資料夾在 Google Drive 同步夾裡,
# 衝突時會自己長出 monitor (1).js 這種副本 —— 掃 js 會把它載進來,而它跟正版
# 定義同名函式、又排在正版後面(字母序),會靜默蓋掉正版的實作。
# 沒有對應面板的 js 一律不載入,備份檔放在 web/js/ 裡是安全的。
_PARTIAL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# util 要先定義 registerTab 給各模組用,main 是啟動者(它要看到完整的註冊表),
# 所以這兩支固定在頭尾,中間的頁籤模組依面板檔名字母序。
_JS_FIRST = "util.js"
_JS_LAST = "main.js"


def _tab_modules() -> list:
    """web/partials/ 裡合格的頁籤模組名,字母序。

    合格 = 檔名符合 _PARTIAL_NAME_RE(小寫英數與底線)且 web/js/ 有同名的 .js。
    兩個條件都是為了擋 Google Drive 的同步衝突副本(monitor (1).html 含空白與括號、
    且不會有配對的 js)。缺一半的不算模組:只有面板沒有 js 的面板不注入(它沒有
    registerTab、按鈕不會出現,注入了只是一塊點不到的 DOM);只有 js 沒有面板的不載入。
    """
    names = []
    for f in sorted((WEB_DIR / "partials").glob("*.html")):
        if not _PARTIAL_NAME_RE.match(f.stem):
            continue
        if not (WEB_DIR / "js" / f"{f.stem}.js").exists():
            continue
        names.append(f.stem)
    return names


def render_index(version: str) -> bytes:
    """讀 web/index.html,把三個標記換成實際內容後回傳 bytes。

      <!--#include-partials-->   掃 web/partials/ 注入各頁面板
      <!--#include-scripts-->    產生 <script> 標籤(util 最先、main 最後)
      <!--#version-->            換成版號

    新增一個頁籤 = 丟 web/partials/xxx.html + web/js/xxx.js 兩個新檔,
    現有檔案零改動(頁籤按鈕由前端的 registerTab 註冊表生成)。

    版號由呼叫端傳入而非在此匯入:VERSION 的唯一來源是 traffic_monitor.py,
    若在這裡匯入它就會形成 traffic_monitor → routes → traffic_monitor 的循環。"""
    html = (WEB_DIR / "index.html").read_bytes()
    mods = _tab_modules()

    panels = b"\n".join((WEB_DIR / "partials" / f"{n}.html").read_bytes() for n in mods)
    html = html.replace(b"<!--#include-partials-->", panels)

    js = [_JS_FIRST] + [f"{n}.js" for n in mods] + [_JS_LAST]
    tags = "\n".join(f'<script src="js/{n}"></script>' for n in js)
    html = html.replace(b"<!--#include-scripts-->", tags.encode("utf-8"))

    # 把版本佔位符換成單一來源的 VERSION(改版只改 traffic_monitor 的 VERSION 常數)。
    html = html.replace(b"<!--#version-->", version.encode("utf-8"))
    return html


# ============================================================
# GET 端點
# ============================================================
def status_payload(app) -> dict:
    """當日流量 / 即時速率 / 各網卡明細 / 健康狀態 / 暴增事件(前端每 5 秒輪詢)。"""
    snap = app.monitor.snapshot()
    # mtime 當快篩(沒檔變新就免讀檔),變新才比對內容雜湊確認:
    # Google Drive 同步常動 mtime 但內容沒變,只看 mtime 會誤報「需重啟」。
    snap["needs_restart"] = needs_restart()
    h = health_snapshot()
    snap["server_errors"] = h["count"]
    snap["recent_errors"] = h["errors"]
    return snap


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def traffic_log(app, date=None, after=None) -> dict:
    """某天的逐分鐘流量明細(舊到新),外加有明細的日期清單供網頁切換。
    明細由伺服器每次取樣寫檔,網頁有沒有開著都照記。
    after = 網頁已有的筆數:只回傳那之後的(rows),total 是那天的總筆數,網頁據此判斷接不接得上。"""
    today = app.monitor.snapshot()["date"]
    if not date or not _DATE_RE.match(date):
        date = today
    after = int(after) if after and str(after).isdigit() else 0
    dates = traffic_log_dates()
    if today not in dates:
        dates.insert(0, today)
    rows, total = app.monitor.traffic_log(date, after)
    return {"date": date, "dates": dates, "after": after, "total": total, "rows": rows}


def traffic_daily(app) -> dict:
    """每日用量統計(總量與 ssh / VS Code / Claude Code),供流量報表。"""
    return app.monitor.daily_stats()


def config_payload(app) -> dict:
    """網頁啟動時要的設定:額度、速率下限、本機 UI 偏好。"""
    cfg = app.cfg
    # 本機 UI 偏好(用量通知);未設過回 None,讓前端沿用當下狀態並回寫種子。
    prefs = load_prefs()
    return {
        "quota_gb": cfg["quota_gb"],
        "machine": machine_label(MACHINE_ID),
        "speed_floor_mbps": cfg.get("speed_floor_mbps", 5.0),
        "peak_window_min": cfg.get("peak_window_min", 10),
        "notify_enabled": prefs.get("notify_enabled"),
        "notify_threshold": prefs.get("notify_threshold"),
        "surge_enabled": prefs.get("surge_enabled"),
        "surge_min_mb": prefs.get("surge_min_mb"),
        "theme": prefs.get("theme"),
    }


# ============================================================
# POST 端點
# ============================================================
def prefs_write(app, body: dict) -> dict:
    """網頁 UI 偏好(用量通知開關/門檻)寫回本機偏好檔,重開即還原(各台獨立)。"""
    patch = {}
    if "notify_enabled" in body:
        patch["notify_enabled"] = bool(body["notify_enabled"])
    if "notify_threshold" in body:
        try:
            patch["notify_threshold"] = min(100, max(1, int(body["notify_threshold"])))
        except (TypeError, ValueError):
            pass
    # 流量暴增預警:開關 + 門檻(MB)。寫回偏好的同時即時套到執行中的 Monitor,
    # 不必重啟就生效(關掉就連程序探測都省去)。
    if "surge_enabled" in body:
        patch["surge_enabled"] = bool(body["surge_enabled"])
        app.monitor.surge_enabled = patch["surge_enabled"]
    if "surge_min_mb" in body:
        try:
            mb = max(1, int(body["surge_min_mb"]))
            patch["surge_min_mb"] = mb
            app.monitor.surge_min_bytes = mb * 1024 * 1024
        except (TypeError, ValueError):
            pass
    # 外觀(深色/淺色/原版)。純顯示偏好,不影響任何計量邏輯。
    # 白名單比對而非照單全收:這個值會直接回給下一次開頁決定載哪支 CSS,
    # 擋掉手工 POST 塞進對應不到的字串。
    if "theme" in body:
        if str(body["theme"]) in ("dark", "light", "classic"):
            patch["theme"] = str(body["theme"])
    prefs = save_prefs(patch) if patch else load_prefs()
    return {"ok": True, "prefs": prefs}


def speedtest(app, body: dict) -> dict:
    """主動測速:抓一小段量實際速率,判斷是否疑似被限速。前端可帶 floor_mbps 覆蓋門檻。"""
    return run_speedtest(app.cfg, body.get("floor_mbps"))


# ============================================================
# 690 資轉監控
# ============================================================
# 不在模組層級 import xfer690:這個檔會同步到 TIPO flow check,那邊沒有那個套件。
_XFER_OFF = {"ok": False, "enabled": False, "message": "690 監控模組載入失敗"}


def _json_only(handler):
    """690 的 POST 會抓信、登入、寫 KM:只收 application/json。

    別的網站的頁面不經 CORS 預檢只能送 text/plain / 表單;要送 application/json 就得先預檢,
    而這個伺服器不回預檢 —— 擋掉跨站觸發寫入。"""
    def wrapped(req):
        if req.content_type != "application/json":
            raise HttpError(415, "要用 Content-Type: application/json")
        return handler(req)
    return wrapped


def xfer690_status(app) -> dict:
    return app.xfer690.snapshot() if app.xfer690 else dict(_XFER_OFF)


def xfer690_refresh(app, body: dict) -> dict:
    """「立即更新」:前景抓一次信,回傳新的快照。網頁一打開也自動抓一次(kind: auto)。"""
    kind = "auto" if body.get("kind") == "auto" else "manual"
    return app.xfer690.poll(kind) if app.xfer690 else dict(_XFER_OFF)


def xfer690_log(app, msg_id) -> Raw:
    """那封通知信附的 .log(伺服器當下抓,不存檔)。"""
    if not app.xfer690:
        raise HttpError(404, _XFER_OFF["message"])
    try:
        text = app.xfer690.log(msg_id or "")
    except KeyError:
        raise HttpError(404, "找不到這封信")
    return Raw(text.encode("utf-8"), "text/plain; charset=utf-8")


def xfer690_wiki_login(app, body: dict) -> dict:
    """使用者貼上的 KM jwt(Innovue OAuth 登入後從瀏覽器 Cookies 複製)。"""
    return app.xfer690.wiki_login(body.get("jwt")) if app.xfer690 else dict(_XFER_OFF)


def xfer690_wiki_logout(app) -> dict:
    return app.xfer690.wiki_logout() if app.xfer690 else dict(_XFER_OFF)


def xfer690_gmail_authorize(app) -> dict:
    """Google 同意頁的網址(網頁在新分頁開);同意後 Google 導回下面的 callback。"""
    if not app.xfer690:
        return dict(_XFER_OFF)
    return app.xfer690.gmail_authorize(f"http://127.0.0.1:{int(app.cfg['port'])}/api/xfer690/gmail/callback")


def xfer690_gmail_callback(app, query: dict) -> Raw:
    """Google 同意頁導回來(帶 code 和 state)。回一句話給那個分頁看。"""
    if not app.xfer690:
        raise HttpError(404, _XFER_OFF["message"])
    msg = app.xfer690.gmail_callback(query)
    return Raw(msg.encode("utf-8"), "text/plain; charset=utf-8")


def xfer690_gmail_logout(app) -> dict:
    return app.xfer690.gmail_logout() if app.xfer690 else dict(_XFER_OFF)


def xfer690_wiki_write(app, body: dict) -> dict:
    """用伺服器記憶體裡的登入寫入 KM。沒登入回 409,不會自己登入。"""
    if not app.xfer690:
        raise HttpError(404, _XFER_OFF["message"])
    try:
        return app.xfer690.wiki_write(body.get("edits") or [])
    except Exception as e:
        if type(e).__name__ == "NotLoggedIn":
            raise HttpError(409, {"ok": False, "error": "not_logged_in"})
        raise


# ============================================================
# 路由表
# ============================================================
def build_routes(app: App) -> dict:
    """端點註冊表。新增端點只要在這裡加一行。路徑是精確比對,順序無關。"""
    return {
        ("GET",  "/api/status"):        lambda req: status_payload(app),
        ("GET",  "/api/traffic/log"):   lambda req: traffic_log(app, req.query.get("date"), req.query.get("after")),
        ("GET",  "/api/traffic/daily"): lambda req: traffic_daily(app),
        ("GET",  "/api/config"):        lambda req: config_payload(app),
        ("POST", "/api/prefs"):         lambda req: prefs_write(app, req.body),
        ("POST", "/api/speedtest"):     lambda req: speedtest(app, req.body),
        ("GET",  "/api/xfer690"):            lambda req: xfer690_status(app),
        ("POST", "/api/xfer690/refresh"):    _json_only(lambda req: xfer690_refresh(app, req.body)),
        ("GET",  "/api/xfer690/log"):        lambda req: xfer690_log(app, req.query.get("id")),
        ("POST", "/api/xfer690/wiki/login"):  _json_only(lambda req: xfer690_wiki_login(app, req.body)),
        ("POST", "/api/xfer690/wiki/logout"): _json_only(lambda req: xfer690_wiki_logout(app)),
        ("POST", "/api/xfer690/gmail/authorize"): _json_only(lambda req: xfer690_gmail_authorize(app)),
        ("GET",  "/api/xfer690/gmail/callback"):  lambda req: xfer690_gmail_callback(app, req.query),
        ("POST", "/api/xfer690/gmail/logout"):    _json_only(lambda req: xfer690_gmail_logout(app)),
        ("POST", "/api/xfer690/wiki/write"): _json_only(lambda req: xfer690_wiki_write(app, req.body)),
    }
