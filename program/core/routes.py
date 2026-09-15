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

from common.jsonio import read_json
from common.webapp import HttpError
from core.health import health_snapshot, needs_restart
from core.paths import BASE, MACHINE_DATA_DIR, MACHINE_ID, WEB_DIR
from core.quota import api_case_hourly, api_quota_status
from core.sampler import run_speedtest
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
# index.html 用 <!--#include partials/xxx.html--> 標記各分頁;送頁時即時換成檔案內容。
# (每次請求重讀,改 partial 重整即生效;不必重啟。)
_INCLUDE_RE = re.compile(rb"<!--#include\s+([\w./-]+)\s*-->")

# 月份參數只接受 YYYY-MM。舊版是用 regex 從 query string 裡撈,撈不到就是 None(當月);
# 改用底座解析 query 之後,格式檢查要自己做 —— 否則 ?month=xyz 會被原樣送進
# trend(),回一個空的月份而不是像舊版那樣退回當月。
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def render_index(version: str) -> bytes:
    """讀 web/index.html,把 <!--#include partials/xxx.html--> 換成該檔內容後回傳 bytes。
    只允許 web/ 內、副檔名 .html 的檔(擋路徑穿越);找不到就留一句註解,不中斷整頁。

    版號由呼叫端傳入而非在此匯入:VERSION 的唯一來源是 traffic_monitor.py,
    若在這裡匯入它就會形成 traffic_monitor → routes → traffic_monitor 的循環。"""
    html = (WEB_DIR / "index.html").read_bytes()
    web_root = WEB_DIR.resolve()

    def repl(m):
        rel = m.group(1).decode("utf-8")
        target = (WEB_DIR / rel).resolve()
        try:
            target.relative_to(web_root)
        except ValueError:
            return b"<!-- include blocked: " + m.group(1) + b" -->"
        if target.suffix != ".html" or not target.exists():
            return b"<!-- include not found: " + m.group(1) + b" -->"
        return target.read_bytes()

    html = _INCLUDE_RE.sub(repl, html)
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
    return snap


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
        "reserve_mb": cfg["reserve_mb"],
        "max_count_per_run": cfg["max_count_per_run"],
        "max_queries_per_campaign": cfg.get("max_queries_per_campaign", 0),
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
        body.get("reserve_mb", app.cfg["reserve_mb"]),
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
