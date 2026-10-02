"""HTTP 端點。

每個端點一個函式，最下方以一張表註冊。新增端點 = 寫一個函式 + 加一行註冊。

路由由 common.webapp 以 (method, path) 精確比對分派，所以註冊順序無關緊要 ——
不必再像舊版 startswith 長鏈那樣，得記得把 /api/monthly/trend 排在
/api/monthly 之前。

端點函式只寫正常路徑：例外由底座統一攔截、統一記進 health.record_error。
要回非 200 狀態碼時拋 webapp.HttpError。
"""

import hashlib
import re
from datetime import datetime

from common.jsonio import file_sig, read_json
from common.webapp import HttpError
from core.health import health_snapshot, needs_restart
from core.paths import BASE, MACHINE_DATA_DIR, MACHINE_ID, WEB_DIR
from core.quota import api_case_hourly, api_quota_status
from core.sampler import run_speedtest, traffic_log_dates
from core.storage import (_gpss_module, gpss_path, load_prefs, load_run_elapsed,
                          machine_label, save_prefs, scan_output_daily)

try:
    from core import monthly_progress   # 月進度計算(讀 data/ 下手動下載的後台匯出檔)
except Exception:
    monthly_progress = None


class App:
    """持有伺服器執行期的三個物件，讓端點函式能取用。

    取代舊版把 monitor / runner / config 掛成 Handler 類別屬性的做法 ——
    那是全域可變狀態，且讓 Handler 無法被獨立測試。
    """

    __slots__ = ("cfg", "monitor", "runner")

    def __init__(self, cfg, monitor, runner):
        self.cfg = cfg
        self.monitor = monitor
        self.runner = runner


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

# 月份參數只接受 YYYY-MM。舊版是用 regex 從 query string 裡撈,撈不到就是 None(當月);
# 改用底座解析 query 之後,格式檢查要自己做 —— 否則 ?month=xyz 會被原樣送進
# trend(),回一個空的月份而不是像舊版那樣退回當月。
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


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
# 爬取統計的資料組裝
# ============================================================
def analytics_payload(cfg: dict, monitor, runner=None) -> dict:
    daily_q = scan_output_daily(cfg)
    daily_e = load_run_elapsed()
    today = datetime.now().strftime("%Y-%m-%d")
    # 把「目前這批正在跑、還沒寫進 run_log」的執行秒數即時補進今天(總計與本機明細),
    # 統計頁的執行時間才會跟著正在跑的任務同步累加。
    live = runner.live_elapsed_today() if runner else 0.0
    if live:
        de = daily_e.setdefault(today, {"total": 0.0, "machines": {}})
        de["total"] += live
        lbl = machine_label(MACHINE_ID)
        de["machines"][lbl] = de["machines"].get(lbl, 0.0) + live

    def merge(date: str) -> dict:
        """把某日的筆數/流量(來自 CSV)與執行時間(來自 run_log)合併,
        並逐機器拆出 {label: {queries, bytes, elapsed_sec}};總計另附。"""
        q = daily_q.get(date, {"queries": 0, "bytes": 0, "machines": {}})
        e = daily_e.get(date, {"total": 0.0, "machines": {}})
        machines = {}
        for lb in set(q["machines"]) | set(e["machines"]):
            mq = q["machines"].get(lb, {})
            machines[lb] = {
                "queries": mq.get("queries", 0),
                "bytes": mq.get("bytes", 0),
                "elapsed_sec": round(e["machines"].get(lb, 0.0), 1),
            }
        return {
            "queries": q["queries"],
            "bytes": q["bytes"],
            "elapsed_sec": round(e["total"], 1),
            "machines": machines,
        }

    dates = sorted(set(daily_q) | set(daily_e), reverse=True)
    history = [dict(date=d, **merge(d)) for d in dates[:31]]
    snap = monitor.snapshot()
    t = merge(today)

    # 本月「我們自己跑掉」的檢索:把本月每一天的 merge() 加總(含今天)。
    # 刻意不從 history 加總 —— 那只留最近 31 天,月初跨月時會漏掉本月前幾天。
    # 這個數字與「檢索進度」頁的本月累積是兩回事:那邊是後台匯出檔的全站總量
    # (含民眾使用),這邊只算這套工具實際送出的檢索,兩者不該互相對帳。
    ym = today[:7]
    month = {"ym": ym, "queries": 0, "bytes": 0, "elapsed_sec": 0.0, "machines": {}}
    for d in sorted(set(daily_q) | set(daily_e)):
        if not d.startswith(ym):
            continue
        one = merge(d)
        month["queries"] += one["queries"]
        month["bytes"] += one["bytes"]
        month["elapsed_sec"] += one["elapsed_sec"]
        for lb, mv in one["machines"].items():
            acc = month["machines"].setdefault(
                lb, {"queries": 0, "bytes": 0, "elapsed_sec": 0.0})
            acc["queries"] += mv["queries"]
            acc["bytes"] += mv["bytes"]
            acc["elapsed_sec"] += mv["elapsed_sec"]
    month["elapsed_sec"] = round(month["elapsed_sec"], 1)
    for mv in month["machines"].values():
        mv["elapsed_sec"] = round(mv["elapsed_sec"], 1)

    # 本月 API 查詢筆數(依驗證碼分組,名稱遮罩)— 來自後台手動下載的 API 匯出檔。
    api = {"codes": [], "total": 0, "data_date": ""}
    if monthly_progress is not None:
        try:
            api = monthly_progress.api_breakdown()
        except Exception:
            pass

    return {
        "today": {
            "date": today,
            "queries": t["queries"],
            "bytes": t["bytes"],
            "elapsed_sec": t["elapsed_sec"],
            "machines": t["machines"],
            "machine_used_bytes": snap["used_bytes"],
            "quota_bytes": snap["quota_bytes"],
        },
        "month": month,
        "history": history,
        "api": api,
        "quota": api_quota_status(cfg),
        "case_hourly": api_case_hourly(cfg),
    }


def _data_sig(cfg: dict) -> str:
    """檢索資料的輕量變動簽章:只看相關檔案的 mtime+size(不解析 Excel,極便宜),
    再加上「當前小時」桶。前端據此判斷「有沒有新的檢索紀錄/該不該更新統計」:
      ‧ 本機寫入或對方資料經雲端同步進來 → summary/run_log 變動 → 簽章變。
      ‧ 跨整點 / 跨 08:00、18:00 時段 → 小時桶變 → 簽章變(讓每小時、時段額度重置也反映)。
    簽章沒變,前端就完全不重抓、不重畫統計 —— 不再每 5 秒無意義刷新。"""
    parts = []
    try:
        out_dir = BASE / cfg["gpss_dir"] / "output"
        for tag, pat in (("x", "summary_*.xlsx"), ("c", "summary_*.csv")):
            for p in sorted(out_dir.glob(pat)):
                try:
                    st = p.stat()
                    parts.append(f"{tag}{p.name}:{int(st.st_mtime)}:{st.st_size}")
                except OSError:
                    pass
    except Exception:
        pass
    try:
        for p in sorted(MACHINE_DATA_DIR.glob("run_log*.jsonl")):
            try:
                st = p.stat()
                parts.append(f"r{p.name}:{int(st.st_mtime)}:{st.st_size}")
            except OSError:
                pass
    except Exception:
        pass
    parts.append("h" + datetime.now().strftime("%Y%m%d%H"))   # 每小時/時段邊界也算一次變動
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()[:16]


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
    # 檢索資料的變動簽章:前端只在它變了才刷新爬取統計(避免每 5 秒盲刷)。
    snap["data_sig"] = _data_sig(app.cfg)
    # 檢索清單的變動簽章,與 data_sig 分開:patents.txt 是「輸入」,summary/run_log 是
    # 「產出」,兩者變動的時機完全不同 —— 併成一個簽章會讓對方存一次清單就害這台
    # 重算一輪統計。分開之後,清單變動只觸發清單重讀(見 monitor.js 的 tickStatus)。
    # 這是兩台共用同一個 Google Drive 資料夾時唯一會「一邊改、另一邊看不到」的資料:
    # 別的都各機一檔(state_<機器>.json 等),只有清單是兩台共寫同一個檔。
    snap["patents_sig"] = file_sig(gpss_path(app.cfg, "patents.txt"))
    return snap


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def traffic_log(app, date=None) -> dict:
    """某天的逐分鐘流量明細(舊到新),外加有明細的日期清單供網頁切換。
    明細由伺服器每次取樣寫檔,網頁有沒有開著都照記。"""
    today = app.monitor.snapshot()["date"]
    if not date or not _DATE_RE.match(date):
        date = today
    dates = traffic_log_dates()
    if today not in dates:
        dates.insert(0, today)
    return {"date": date, "dates": dates, "rows": app.monitor.traffic_log(date)}


def traffic_daily(app) -> dict:
    """每日用量統計(總量與 ssh / VS Code / Claude Code),供流量報表。"""
    return app.monitor.daily_stats()


def job_status(app) -> dict:
    """檢索任務的即時狀態(前端輪詢用)。"""
    return app.runner.status()


def config_payload(app) -> dict:
    """網頁啟動時要的設定:額度、上限、API 是否設好、本機 UI 偏好。"""
    cfg = app.cfg
    # 本機 API 設定優先(api_config_<機器>.json),沒有才看共用的 api_config.json。
    ac = {}
    for name in ("api_config.json", f"api_config_{MACHINE_ID}.json"):
        apath = gpss_path(cfg, name)
        if apath.exists():
            one = read_json(apath, default={})
            if isinstance(one, dict):
                ac.update(one)
    api_set = bool(str(ac.get("userCode", "")).strip())
    patdb = ac.get("patDB", "")
    # 本機 UI 偏好(用量通知);未設過回 None,讓前端沿用當下狀態並回寫種子。
    prefs = load_prefs()
    return {
        "quota_gb": cfg["quota_gb"],
        "max_count_per_run": cfg["max_count_per_run"],
        "userCode_set": api_set,
        "patDB": patdb,
        "machine": machine_label(MACHINE_ID),
        "speed_floor_mbps": cfg.get("speed_floor_mbps", 5.0),
        "peak_window_min": cfg.get("peak_window_min", 10),
        "notify_enabled": prefs.get("notify_enabled"),
        "notify_threshold": prefs.get("notify_threshold"),
        "surge_enabled": prefs.get("surge_enabled"),
        "surge_min_mb": prefs.get("surge_min_mb"),
        "exclude_lan": prefs.get("exclude_lan"),
        "theme": prefs.get("theme"),
    }


def analytics(app) -> dict:
    """爬取統計頁的全部資料(今日/歷史/API 分組/額度/時段)。"""
    return analytics_payload(app.cfg, app.monitor, app.runner)


def monthly_trend(month=None) -> dict:
    """達標分析趨勢:只讀取本功能啟用後、使用者主動更新所留下的快照,不回推舊資料。

    失敗時回 200 加空陣列(不是 500):前端直接拿這些欄位渲染,
    給它 500 會讓整張圖連同月份下拉一起消失。
    """
    if monthly_progress is None:
        return {"ok": False, "points": [], "snapshots": [], "months": [],
                "message": "monthly_progress 模組載入失敗"}
    if month is not None and not _MONTH_RE.match(month):
        month = None      # 格式不對就當沒給,退回當月(與舊版的 regex 撈不到時相同)
    try:
        return {"ok": True, **monthly_progress.trend(month)}
    except Exception as e:
        return {"ok": False, "points": [], "snapshots": [], "months": [],
                "message": f"讀取趨勢失敗:{e}"}


def monthly_now() -> dict:
    """月進度:讀 data/ 下手動下載的後台匯出檔(簡易版 + API),加總檢索次數。

    同 monthly_trend:失敗回 200 加 files_found=False,前端靠這個欄位顯示提示。
    """
    if monthly_progress is None:
        return {"ok": False, "files_found": False,
                "message": "monthly_progress 模組載入失敗"}
    try:
        return monthly_progress.compute()
    except Exception as e:
        return {"ok": False, "files_found": False,
                "message": f"計算失敗:{e}"}


def keys_list() -> dict:
    """驗證碼池(只回遮罩碼,不外洩明碼)+ 本機目前選用哪一組。"""
    g = _gpss_module()
    if g is None:
        return {"ok": False, "keys": [], "message": "驗證碼模組載入失敗"}
    try:
        keys = g.load_keys()
        cur = g.resolve_current_key(keys)
        return {
            "ok": True,
            "active": (cur or {}).get("id"),
            "keys": [{"id": k.get("id"), "label": k.get("label", ""),
                      "masked": g._mask_code(k.get("userCode", ""))} for k in keys],
        }
    except Exception as e:
        return {"ok": False, "keys": [], "message": str(e)}


def patents_read(app) -> dict:
    """讀檢索清單(patents.txt);# 開頭與空行不計入筆數。"""
    p = gpss_path(app.cfg, "patents.txt")
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    count = sum(1 for l in text.splitlines()
                if l.strip() and not l.strip().startswith("#"))
    return {"ok": True, "text": text, "count": count}


# ============================================================
# POST 端點
# ============================================================
def monthly_refresh() -> dict:
    """只有使用者明確按「更新計算」才留下達標趨勢快照;讀取/月統計輪詢一律不寫。"""
    if monthly_progress is None:
        raise HttpError(500, {"ok": False, "files_found": False,
                              "message": "monthly_progress 模組載入失敗"})
    # 三步全包在同一個 try 裡:前端靠 files_found 這個欄位決定顯示什麼,
    # 讓底座統一收斂會回成沒有該欄位的通用 500,畫面就少一段提示。
    try:
        progress = monthly_progress.compute()
        snap = monthly_progress.record_snapshot(progress)
        return {**progress, "snapshot": snap,
                "trend": monthly_progress.trend(progress.get("month"))}
    except Exception as e:
        raise HttpError(500, {"ok": False, "files_found": False,
                              "message": f"更新計算失敗:{e}"})


def job_start(app, body: dict) -> dict:
    """開始檢索。已有任務在跑時回 409。"""
    ok, msg = app.runner.start(
        body.get("mode", "count"),
        body.get("count"),
        body.get("limit_detect", True),
        body.get("method", "api"),
    )
    if not ok:
        raise HttpError(409, {"ok": False, "message": msg})
    return {"ok": True, "message": msg}


def job_stop(app) -> dict:
    """停止正在跑的檢索。"""
    ok, msg = app.runner.stop()
    return {"ok": ok, "message": msg}


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
    if "exclude_lan" in body:
        patch["exclude_lan"] = bool(body["exclude_lan"])
        app.monitor.exclude_lan_enabled = patch["exclude_lan"]
    # 外觀(深色/淺色/原版)。純顯示偏好,不影響任何計量邏輯。
    # 白名單比對而非照單全收:這個值會直接回給下一次開頁決定載哪支 CSS,
    # 擋掉手工 POST 塞進對應不到的字串。
    if "theme" in body:
        if str(body["theme"]) in ("dark", "light", "classic"):
            patch["theme"] = str(body["theme"])
    prefs = save_prefs(patch) if patch else load_prefs()
    return {"ok": True, "prefs": prefs}


def keys_write(body: dict) -> dict:
    """action="add":新增一組到驗證碼池;否則(帶 id)= 切換本機選用的驗證碼。"""
    g = _gpss_module()
    if g is None:
        raise HttpError(500, "驗證碼模組載入失敗")
    if body.get("action") == "add":
        label = (body.get("label") or "").strip()
        uc = (body.get("userCode") or "").strip()
        if not uc:
            raise HttpError(400, "請輸入驗證碼")
        keys = g.load_keys()
        dup = next((k for k in keys if k.get("userCode") == uc), None)
        if dup:
            raise HttpError(409, f"此驗證碼已在池中:[{dup.get('id')}] {dup.get('label','')}")
        used = {str(k.get("id", "")).upper() for k in keys}
        nid = next((chr(c) for c in range(ord("A"), ord("Z") + 1)
                    if chr(c) not in used), f"K{len(keys)}")
        nk = {"id": nid, "label": label or nid, "userCode": uc}
        keys.append(nk)
        g.save_keys(keys)
        return {"ok": True, "id": nid, "label": nk["label"], "masked": g._mask_code(uc)}

    kid = body.get("id")
    keys = g.load_keys()
    match = next((k for k in keys if k.get("id") == kid), None)
    if not match:
        raise HttpError(404, f"找不到驗證碼 {kid}")
    g.write_active_key(match)
    return {"ok": True, "active": match.get("id"),
            "label": match.get("label", ""),
            "masked": g._mask_code(match.get("userCode", ""))}


def clear_warning(app) -> dict:
    """使用者按掉警告橫幅:清掉伺服器端的警告,避免重整網頁後又跳回來。"""
    app.runner.clear_warning()
    return {"ok": True}


def speedtest(app, body: dict) -> dict:
    """主動測速:抓一小段量實際速率,判斷是否疑似被限速。前端可帶 floor_mbps 覆蓋門檻。"""
    return run_speedtest(app.cfg, body.get("floor_mbps"))


def patents_write(app, body: dict) -> dict:
    """存檢索清單(patents.txt)。"""
    text = body.get("text", "")
    p = gpss_path(app.cfg, "patents.txt")
    p.write_text(text, encoding="utf-8")
    count = sum(1 for l in text.splitlines()
                if l.strip() and not l.strip().startswith("#"))
    return {"ok": True, "count": count, "message": f"已儲存 {count} 筆"}


# ============================================================
# 路由表
# ============================================================
def build_routes(app: App) -> dict:
    """端點註冊表。新增端點只要在這裡加一行。

    路徑是精確比對,順序無關 —— /api/monthly 與 /api/monthly/trend
    是兩個獨立的鍵,不會互相吞噬。
    """
    return {
        ("GET",  "/api/status"):          lambda req: status_payload(app),
        ("GET",  "/api/traffic/log"):     lambda req: traffic_log(app, req.query.get("date")),
        ("GET",  "/api/traffic/daily"):   lambda req: traffic_daily(app),
        ("GET",  "/api/job"):             lambda req: job_status(app),
        ("GET",  "/api/config"):          lambda req: config_payload(app),
        ("GET",  "/api/analytics"):       lambda req: analytics(app),
        ("GET",  "/api/monthly"):         lambda req: monthly_now(),
        ("GET",  "/api/monthly/trend"):   lambda req: monthly_trend(req.query.get("month")),
        ("GET",  "/api/keys"):            lambda req: keys_list(),
        ("GET",  "/api/patents"):         lambda req: patents_read(app),
        ("POST", "/api/monthly/refresh"): lambda req: monthly_refresh(),
        ("POST", "/api/run"):             lambda req: job_start(app, req.body),
        ("POST", "/api/stop"):            lambda req: job_stop(app),
        ("POST", "/api/prefs"):           lambda req: prefs_write(app, req.body),
        ("POST", "/api/keys"):            lambda req: keys_write(req.body),
        ("POST", "/api/clearwarn"):       lambda req: clear_warning(app),
        ("POST", "/api/speedtest"):       lambda req: speedtest(app, req.body),
        ("POST", "/api/patents"):         lambda req: patents_write(app, req.body),
    }
