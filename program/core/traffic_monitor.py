"""
Traffic Monitor + GPSS launcher
================================

A small always-local web app with two halves:

  1. 流量監控 — reads cumulative NIC byte counters (all adapters summed)
     via psutil, accumulates the day's usage against a daily quota
     (default 2GB up+down), and serves it as a live web page.

  2. 控制面板 — launches the GPSS query script with parameters and safety
     guardrails (reserve floor + per-run cap), with live progress and a stop
     button. Two methods are offered: "api" (gpss_api_query.py, official API)
     and "web" (gpss_web_query.py, traditional front-end via Playwright). The
     query logic itself is NOT touched here; this only wraps it.

Why read cumulative counters (not sampled rate): the counter is monotonic,
so "current - last" is exact regardless of how often we poll — no traffic
is missed between samples. Reboots reset the counter; we detect that
(current < last) and treat the current value as the post-reset delta.

Run:   python program/core/traffic_monitor.py   →   http://127.0.0.1:8765
Stop:  Ctrl+C in this window.
"""

import os
import re
import sys
import json
import math
import time
import hashlib
import threading
import webbrowser
import collections
import ipaddress
import subprocess
import urllib.request
from pathlib import Path
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil

# 別把 .pyc 寫進這個資料夾 —— 它在 Google Drive 同步夾裡,兩台機器(且 Python 版本不同)
# 各自產生的 __pycache__/*.pyc 會互相同步、產生衝突複本,白白佔用同步頻寬、拖慢真資料同步。
# 須在匯入 monthly_progress / output_store 之前設定,才能擋掉它們的 .pyc。
sys.dont_write_bytecode = True

try:
    import monthly_progress   # 月進度計算(讀 data/ 下手動下載的後台匯出檔)
except Exception:
    monthly_progress = None


# Windows ESTATS(每條 TCP 連線的精準收/發位元組)已抽到獨立模組 estats.py。
# 對外只用到這兩個函式:process_is_elevated() 判是否有提權、estats_probe() 取逐連線位元組。
from estats import process_is_elevated, estats_probe
# 路徑/機器身分常數抽到 paths.py;資料層(設定/偏好/機器標籤/output 統計掃描)抽到 storage.py。
# 兩者皆為單向相依的下層模組(本檔 → paths/storage),不會回頭匯入本檔,避免循環匯入。
from paths import (BASE, DATA_DIR, MACHINE_DATA_DIR, MACHINES_PATH, WEB_DIR, MACHINE_ID,
                   RUN_LOG_PATH, STATE_PATH)
from storage import (output_store, load_prefs, save_prefs, machine_label,
                     load_config, gpss_path, _gpss_module,
                     scan_output_daily, load_run_elapsed)
# API 額度 / 驗證碼歸戶抽到 quota.py(相依 paths + storage,不回頭匯入本檔)。
from quota import api_quota_status, api_case_hourly


# 系統版本(語意化版本控制)。這是唯一來源:網頁底部的「系統版本」會由
# _render_index() 把 index.html 裡的 <!--#version--> 換成這個值,改版只改這裡。
VERSION = "7.2.1"

# index.html 用 <!--#include partials/xxx.html--> 標記各分頁;送頁時即時換成檔案內容。
# (每次請求重讀,改 partial 重整即生效;不必重啟。)
_INCLUDE_RE = re.compile(rb"<!--#include\s+([\w./-]+)\s*-->")


def _render_index():
    """讀 web/index.html,把 <!--#include partials/xxx.html--> 換成該檔內容後回傳 bytes。
    只允許 web/ 內、副檔名 .html 的檔(擋路徑穿越);找不到就留一句註解,不中斷整頁。"""
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
    # 把版本佔位符換成單一來源的 VERSION(改版只改上面的 VERSION 常數)。
    html = html.replace(b"<!--#version-->", VERSION.encode("utf-8"))
    return html

# 監看「在這個程序內執行」的原始碼檔:啟動時記下狀態,之後若磁碟上的檔內容變了,
# 代表妳改了程式、但這個正在跑的實例還是舊版 → 前端顯示「需重啟」。
# (index.html、gpss_*.py 不列入:前者每次請求即時讀檔、後者每次檢索開新行程,
#  改完重新整理或下次執行就生效,不必重啟監控。)
_RESTART_WATCH = [
    Path(__file__).resolve(),
    Path(__file__).resolve().with_name("monthly_progress.py"),
    # 從本檔拆出、且在這個程序內載入的下層模組:改它們同樣要重啟才生效。
    Path(__file__).resolve().with_name("estats.py"),
    Path(__file__).resolve().with_name("paths.py"),
    Path(__file__).resolve().with_name("storage.py"),
    # output_store.py 也在這個程序內載入(統計/額度都靠它),改它一樣要重啟才生效。
    Path(__file__).resolve().parents[2] / "program" / "gpss" / "output_store.py",
]


def _source_mtime() -> float:
    newest = 0.0
    for p in _RESTART_WATCH:
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            pass
    return newest


def _source_hash() -> str:
    """監看檔內容的彙總雜湊。比 mtime 可靠:Google Drive 同步常會更動檔案的修改時間
    但內容沒變(虛擬磁碟重新落地、跨機同步回寫),只看 mtime 會誤判「需重啟」。
    比對內容雜湊就只在『內容真的變了』時才提示。檔案讀不到時以路徑+缺檔標記納入,
    讓「檔案被刪/暫時讀不到」也算一種變動。"""
    h = hashlib.md5()
    for p in _RESTART_WATCH:
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"\x00<missing:" + str(p).encode("utf-8") + b">\x00")
        h.update(b"\n--\n")
    return h.hexdigest()


_STARTUP_MTIME = _source_mtime()
_STARTUP_HASH = _source_hash()


def _needs_restart() -> bool:
    """是否該提示重啟。快路徑:沒有任何監看檔比啟動時新(+0.5s 緩衝避開 mtime 浮點抖動)
    就一定沒改 —— 不必讀檔。只有當有檔 mtime 變新時(可能真改了,也可能只是 Drive 動了
    時間戳)才讀內容算雜湊確認:雜湊不同才算真的需要重啟。"""
    if _source_mtime() <= _STARTUP_MTIME + 0.5:
        return False
    return _source_hash() != _STARTUP_HASH

# ── 伺服器健康狀態 ───────────────────────────────────────────────
# 把原本只 print 到 cmd 的錯誤收進來,讓頁面直接看得到。妳說「跑不出來就去看 cmd
# 一堆錯誤、重開就好」—— 就是要讓妳不必再翻 cmd:錯誤累積/資料停更時頁面會提醒重啟。
# (瀏覽器中途斷線那種無害的 10053/10054 不算進來,以免假警報。)
_HEALTH = {
    "errors": collections.deque(maxlen=20),   # 最近的錯誤訊息(含時間)
    "count": 0,                               # 啟動以來累計筆數
}
_HEALTH_LOCK = threading.Lock()


def record_error(where: str, exc) -> None:
    msg = f"{datetime.now().strftime('%H:%M:%S')} [{where}] {exc}"
    with _HEALTH_LOCK:
        _HEALTH["errors"].append(msg)
        _HEALTH["count"] += 1
    print(f"[warn] {msg}")

# output_store(統計/額度資料來源)與 sys.path 設定已隨 storage.py 一起搬走;
# 本檔用到的 output_store 由上方 `from storage import output_store` 取得。
# 把零散的 summary CSV 併進當月 Excel(consolidate)由「這個常駐伺服器」負責,不再放在
# 檢索子程序裡 —— 那個檔在 Google Drive 同步夾裡可達數百 KB,load+save 整份很慢/易撞鎖。
# 以前是檢索程式印完 summary 後才同步併檔,子程序遲遲不結束 → 網頁的任務狀態一直停在
# 「執行中」、不會自動停(使用者只能手動按停,還可能砍在 save 中途害 CSV 沒併進去)。
# 改由伺服器在「任務結束後」用背景執行緒併檔:子程序印完 summary 就立刻退出 → 任務即時
# 轉「完成」並自動停;併檔在背景慢慢做,從不擋使用者。鎖確保同時最多一個併檔在跑
# (兩次併檔同時 load/save 同一個 Excel 會互相蓋寫/讀到半截)。
_CONSOLIDATE_LOCK = threading.Lock()


def consolidate_async(out_dir):
    """任務結束後在背景把本機 summary CSV 併進當月 Excel;不阻塞、不影響任務狀態。
    已有一個併檔在跑就跳過(殘留 CSV 由統計直接讀得到,下次任務結束時再併也不遲)。"""
    if output_store is None:
        return

    def work():
        if not _CONSOLIDATE_LOCK.acquire(blocking=False):
            return
        try:
            output_store.consolidate(MACHINE_ID, out_dir)
        except Exception as e:
            record_error("consolidate_async", e)
        finally:
            _CONSOLIDATE_LOCK.release()

    threading.Thread(target=work, daemon=True).start()


# DEFAULT_CONFIG / load_config / gpss_path / _gpss_module / scan_output_daily /
# load_run_elapsed 已搬到 storage.py(資料層);本檔由上方 `from storage import ...` 取得。


# API 額度 / 驗證碼歸戶(_mask_code / _load_quota_baseline / _current_segment /
# _code_of_machine / _effective_code / api_quota_status / api_case_hourly)已搬到 quota.py;
# 本檔由上方 `from quota import api_quota_status, api_case_hourly` 取得(analytics 會用)。


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
# 流量監控
# ============================================================
class Monitor:
    """Accumulates per-day network usage (all NICs summed)."""

    def __init__(self, cfg: dict, counters_fn=None):
        self.cfg = cfg
        # 網卡累積計數器的來源。預設讀 psutil;測試時可注入假資料,
        # 才驗證得了跨日歸零與「重開機計數器回捲」這兩段核心邏輯。
        self._counters_fn = counters_fn or (lambda: psutil.net_io_counters(pernic=True))
        self.quota_bytes = float(cfg["quota_gb"]) * (1024 ** 3)
        self.debug_lan = bool(cfg.get("debug_lan", False))
        self.exclude = [x.lower() for x in cfg.get("exclude_nics", [])]
        self.lock = threading.Lock()

        self.date = None
        self.day_sent = 0
        self.day_recv = 0
        self.per_nic = {}        # nic -> [sent, recv]  (today's accumulated)
        self.last_per_nic = {}   # nic -> [sent, recv]  (last cumulative read)
        self.last_time = None
        self.last_sample_at = None   # 最後一次「成功取樣」的牆鐘時間(供頁面判斷是否停更)
        self.rate_up = 0.0
        self.rate_down = 0.0

        # 被動限速偵測:留一段時間內的下載速率,取其最高值當「天花板」。
        # 只有真的在下載時這個峰值才有意義(閒置時偏低,不代表被限速)。
        self.peak_window_sec = float(cfg.get("peak_window_min", 10)) * 60
        self.rate_hist = collections.deque()   # (monotonic_ts, rate_down_bytes_per_sec)

        # ── 流量暴增預警 ──
        # surge_enabled / surge_min_mb 以本機 prefs 為準(頁面可切),其餘參數讀 config。
        interval = max(1, int(cfg.get("sample_interval_sec", 60)))
        prefs = load_prefs()
        se = prefs.get("surge_enabled")
        self.surge_enabled = bool(se) if se is not None else bool(cfg.get("surge_enabled", True))
        mb = prefs.get("surge_min_mb")
        base_mb = float(mb) if mb is not None else float(cfg.get("surge_min_mb", 50))
        self.surge_min_bytes = max(0.0, base_mb) * 1024 * 1024
        self.surge_factor = float(cfg.get("surge_factor", 4.0))
        self.surge_cooldown = float(cfg.get("surge_cooldown_sec", 300))
        win_sec = float(cfg.get("surge_window_min", 30)) * 60
        # 回看視窗存「每次取樣的用量(收+發)」,供中位數基準與暴增判斷;至少留 6 筆。
        self.byte_hist = collections.deque(maxlen=max(6, int(win_sec / interval)))
        # 當日暴增事件:跨重啟保留、跨日歸零。每個事件含當下每條 TCP 連線的 IP／程式／流量，
        # 供網頁「暴增紀錄」頁籤按應用程式／時間收合後逐來源展開；最多留 200 個避免狀態檔無限長大。
        self.surges = collections.deque(maxlen=200)
        self._last_surge_at = 0.0     # 最近一次記錄暴增的 monotonic 時刻(冷卻用)
        self._surge_seq = 0           # 事件流水號(前端據此判斷有沒有「新的」暴增)
        # 逐次取樣的計費明細(供網頁 log 框即時診斷:每筆 = 一次 NIC 結算怎麼分內/外網)。
        # 只保留近 5 分鐘(頁面只顯示 5 分鐘):依取樣間隔換算筆數,至少留 5 筆。
        self.sample_log = collections.deque(maxlen=max(5, (300 + interval - 1) // interval))

        # ── 排除內網流量 ──
        # exclude_lan 以本機 prefs 為準(頁面可切);內網/強制計入網段清單讀 config。
        el = prefs.get("exclude_lan")
        self.exclude_lan_enabled = bool(el) if el is not None else bool(cfg.get("exclude_lan", True))
        self.lan_nets = []
        for c in cfg.get("lan_ranges", []):
            try:
                self.lan_nets.append(ipaddress.ip_network(c, strict=False))
            except ValueError:
                pass
        # 強制計入白名單(優先於 lan_ranges):保護對外/額度流量(如 GPSS 10.65.x.x)。
        self.count_nets = []
        for c in cfg.get("count_ranges", []):
            try:
                self.count_nets.append(ipaddress.ip_network(c, strict=False))
            except ValueError:
                pass
        self.day_lan_excluded = 0     # 今日已排除(內網)位元組;跨重啟保留、跨日歸零

        # ── 高頻 ESTATS 探測(與 60s NIC 計費解耦)──
        # probe_lan() 由獨立的高頻迴圈(lan_probe_interval_sec,預設 1s)呼叫:用 ESTATS 取每條
        # TCP 連線累積的收/發位元組,算出本探測區間的增量 → 內網連線的量累進 pending(供 sample()
        # 消耗、各方向 clamp 到實際 NIC 增量),各 IP／程序來源的外網增量累進 _pending_source_net 供暴增歸因。
        # 需提權才拿得到數字(_estats_ok);沒提權時內網排除與暴增歸因停用,但總流量計費照常。
        self._estats_ok = process_is_elevated()
        self._estats_prev = {}        # conn_key -> (data_in, data_out):上一輪各連線累計(算增量)
        # 自上次 NIC 取樣以來,各連線 ESTATS payload 增量,分內網/外網累計。sample() 用
        # 「內網 payload 占總 payload 的比例」去分攤實際 NIC 增量(含 TCP/IP 標頭、重傳),
        # 比舊的「payload 封頂」更準 —— 內網占滿時可扣掉近 100% NIC,不再漏那 ~4% 標頭。
        self._pending_lan_sent = 0    # 內網連線『上傳』payload
        self._pending_lan_recv = 0    # 內網連線『下載』payload
        self._pending_ext_sent = 0    # 外網連線『上傳』payload(含 GPSS 10.65)
        self._pending_ext_recv = 0    # 外網連線『下載』payload
        # (pid, 對方 IP) -> [程序名, bytes]:自上次 NIC 取樣以來的外網來源增量,供暴增歸因與明細。
        self._pending_source_net = {}
        self._name_cache = {}         # pid -> 程序名(避免每輪都查 psutil)

        self._load_state()

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _excluded(self, nic: str) -> bool:
        n = nic.lower()
        return any(x in n for x in self.exclude)

    def _baseline(self, counters: dict):
        self.last_per_nic = {
            nic: [st.bytes_sent, st.bytes_recv]
            for nic, st in counters.items() if not self._excluded(nic)
        }

    def _load_state(self):
        cur = self._counters_fn()
        today = self._today()
        saved = None
        # 讀新的 state_<機器>.json;若還沒有(剛從舊版升級),沿用舊的單一
        # state.json,讓今天已累積的流量無縫接續(下方網卡比對仍會擋掉別台資料)。
        for path in (STATE_PATH, DATA_DIR / "state.json"):
            if path.exists():
                try:
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    break
                except Exception:
                    saved = None

        # 換電腦偵測:存檔記的是「本機網卡名稱」。若存檔有網卡、卻沒有任何一張
        # 跟現在這台對得上,代表這份 state.json 是別台電腦複製過來的 ——
        # 不要沿用它的當日累計,改從 0 起算(避免顯示到別台電腦的流量)。
        cur_nics = {nic for nic in cur if not self._excluded(nic)}
        saved_nics = set((saved or {}).get("last_per_nic", {})) | set((saved or {}).get("per_nic", {}))
        same_machine = (not saved_nics) or bool(saved_nics & cur_nics)
        if saved and saved.get("date") == today and not same_machine:
            print("[info] 偵測到網卡與存檔不符(可能換了電腦),當日流量改從 0 起算。")

        if saved and saved.get("date") == today and same_machine:
            # Resume today's tally across a restart.
            self.date = today
            self.day_sent = int(saved.get("day_sent", 0))
            self.day_recv = int(saved.get("day_recv", 0))
            self.day_lan_excluded = int(saved.get("day_lan_excluded", 0))
            # 舊版 state 沒有 surges 時自然從空白開始;新版本保存當日完整來源紀錄供頁面展開。
            saved_surges = saved.get("surges", [])
            if isinstance(saved_surges, list):
                self.surges.extend(ev for ev in saved_surges if isinstance(ev, dict))
                self._surge_seq = max((int(ev.get("seq", 0)) for ev in self.surges), default=0)
            self.per_nic = {k: list(v) for k, v in saved.get("per_nic", {}).items()}
            self.last_per_nic = {k: list(v) for k, v in saved.get("last_per_nic", {}).items()}
            # Any NIC present now but not in saved state -> baseline it (don't
            # count its pre-existing cumulative history).
            for nic, st in cur.items():
                if not self._excluded(nic) and nic not in self.last_per_nic:
                    self.last_per_nic[nic] = [st.bytes_sent, st.bytes_recv]
        else:
            # Fresh day (or no/old state): start at zero, baseline from now.
            self.date = today
            self.day_sent = 0
            self.day_recv = 0
            self.day_lan_excluded = 0
            self.surges.clear()
            self._surge_seq = 0
            self.per_nic = {}
            self._baseline(cur)

        self.last_time = time.monotonic()

    def _save_state(self):
        data = {
            "date": self.date,
            "day_sent": self.day_sent,
            "day_recv": self.day_recv,
            "day_lan_excluded": self.day_lan_excluded,
            "surges": list(self.surges),
            "per_nic": self.per_nic,
            "last_per_nic": self.last_per_nic,
        }
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_PATH)

    def sample(self):
        cur = self._counters_fn()
        now = time.monotonic()
        today = self._today()

        with self.lock:
            if today != self.date:
                # Day rollover: zero the day's accumulators, keep last_per_nic
                # continuous so the rollover moment doesn't dump a huge delta.
                self.date = today
                self.day_sent = 0
                self.day_recv = 0
                self.day_lan_excluded = 0
                self.surges.clear()
                self._surge_seq = 0
                self.per_nic = {}
                # 跨日把待消耗的內網/外網/暴增量歸零,免得昨天尾巴的 pending 扣到今天。
                self._pending_lan_sent = 0
                self._pending_lan_recv = 0
                self._pending_ext_sent = 0
                self._pending_ext_recv = 0
                self._pending_source_net = {}

            dt = max(1e-6, now - (self.last_time or now))
            step_sent = 0
            step_recv = 0
            for nic, st in cur.items():
                if self._excluded(nic):
                    continue
                prev = self.last_per_nic.get(nic)
                if prev is None:
                    # NIC appeared mid-run: baseline, don't count its history.
                    self.last_per_nic[nic] = [st.bytes_sent, st.bytes_recv]
                    continue
                ds = st.bytes_sent - prev[0]
                dr = st.bytes_recv - prev[1]
                if ds < 0:  # counter reset (reboot / NIC reset)
                    ds = st.bytes_sent
                if dr < 0:
                    dr = st.bytes_recv
                step_sent += ds
                step_recv += dr
                acc = self.per_nic.setdefault(nic, [0, 0])
                acc[0] += ds
                acc[1] += dr
                self.last_per_nic[nic] = [st.bytes_sent, st.bytes_recv]

            # 內網排除:按「內網 payload 占總 payload 的比例」分攤實際 NIC 增量後扣除。
            # ESTATS 給的是應用層 payload(不含 TCP/IP 標頭、重傳),直接拿來扣會漏掉線路上
            # 那 ~4% 標頭;改成「內網占比 × NIC 增量」就能連標頭一起按比例扣掉。內網占滿時
            # 幾乎扣光整個 NIC 增量;沒抓到任何連線(payload 全 0)時不扣(寧可算到、不漏算)。
            # 即時速率仍反映實際 NIC(供限速判斷);GPSS 連 10.65.x 判為外網、照計。
            lan_sent = lan_recv = 0
            if self.exclude_lan_enabled:
                tot_recv = self._pending_lan_recv + self._pending_ext_recv
                tot_sent = self._pending_lan_sent + self._pending_ext_sent
                if tot_recv > 0:
                    lan_recv = min(step_recv,
                                   int(round(step_recv * self._pending_lan_recv / tot_recv)))
                if tot_sent > 0:
                    lan_sent = min(step_sent,
                                   int(round(step_sent * self._pending_lan_sent / tot_sent)))
                if self.debug_lan:
                    # 逐方向明細:NIC 實際增量 / ESTATS 內網 payload / 對外 payload / 本次實扣。
                    # 收方向實扣應≈NIC(純內網下載);送方向 payload≈0(全是 ACK)→ 實扣會偏低,
                    # 那段 ACK 就是大檔仍被計入的殘量。
                    print(f"[lan] recv: NIC={step_recv} lan_pay={self._pending_lan_recv} "
                          f"ext_pay={self._pending_ext_recv} excl={lan_recv}  |  "
                          f"sent: NIC={step_sent} lan_pay={self._pending_lan_sent} "
                          f"ext_pay={self._pending_ext_sent} excl={lan_sent}")
            # 逐次計費明細(供網頁 log 框)：在 pending 歸零前快照本次結算怎麼分內/外網。
            self.sample_log.append({
                "t": datetime.now().strftime("%H:%M:%S"),
                "dt": round(dt, 1),
                "nic_recv": int(step_recv), "nic_sent": int(step_sent),
                "lan_pay_recv": int(self._pending_lan_recv), "ext_pay_recv": int(self._pending_ext_recv),
                "lan_pay_sent": int(self._pending_lan_sent), "ext_pay_sent": int(self._pending_ext_sent),
                "excl_recv": int(lan_recv), "excl_sent": int(lan_sent),
                "cnt_recv": int(step_recv - lan_recv), "cnt_sent": int(step_sent - lan_sent),
                "exlan": bool(self.exclude_lan_enabled), "estats": bool(self._estats_ok),
            })
            self._pending_lan_sent = self._pending_lan_recv = 0
            self._pending_ext_sent = self._pending_ext_recv = 0
            self.day_sent += step_sent - lan_sent
            self.day_recv += step_recv - lan_recv
            self.day_lan_excluded += lan_sent + lan_recv
            self.rate_up = step_sent / dt
            self.rate_down = step_recv / dt
            self.last_time = now

            # 記下本次下載速率,並丟掉視窗外的舊樣本(供 snapshot 取近期峰值)。
            self.rate_hist.append((now, self.rate_down))
            cutoff = now - self.peak_window_sec
            while self.rate_hist and self.rate_hist[0][0] < cutoff:
                self.rate_hist.popleft()

            self.last_sample_at = datetime.now()

            # 暴增偵測:用『實際計入額度』的量(已扣掉內網),不是網卡原始量 —— 否則內網
            # 下載(NAS/FTP,已排除)會狂報假暴增「用了 X GB」,但其實沒吃額度。歸因用高頻
            # ESTATS 探測自上次 NIC 取樣以來累積的「各 IP／程序來源網路增量」
            # (_pending_source_net),消耗後歸零,下個 NIC 區間重新累積。
            counted_sent = step_sent - lan_sent
            counted_recv = step_recv - lan_recv
            self._detect_surge(counted_sent + counted_recv, counted_sent, counted_recv,
                               dt, self._pending_source_net, now)
            self._pending_source_net = {}

        self._save_state()

    def probe_lan(self):
        """高頻探測(由 probe_loop 每 lan_probe_interval_sec 秒呼叫,預設 1s)。與 60s 的 NIC
        計費解耦:用 ESTATS 取每條 TCP 連線累積的收/發位元組,算出本探測區間的增量 →
        內網連線(對方 IP 落在 lan_ranges、且不在 count_ranges)的量累進 _pending_lan_*(供
        sample() 消耗),各 IP／程序來源的外網增量累進 _pending_source_net(供暴增歸因)。需提權才拿得到
        數字;沒提權或兩功能都關就直接跳過。ESTATS 呼叫不佔鎖,只在更新 pending 時短暫上鎖。"""
        if not self._estats_ok:
            return
        if not (self.surge_enabled or self.exclude_lan_enabled):
            self._estats_prev = {}
            return

        conns = estats_probe()    # {conn_key: (pid, raddr_ip, data_in, data_out)}

        lan_sent = lan_recv = 0
        ext_sent = ext_recv = 0
        source_net = {}           # (pid, 對方 IP) -> bytes(本區間外網增量,供暴增歸因)
        # 第一輪探測(剛啟動、或剛開啟排除把 _estats_prev 清空)時,所有連線都「看似新出現」,
        # 但其累計值可能含啟動前的歷史 → 這一輪只立基準、不灌歷史,避免一次性灌爆比例。
        first_probe = not self._estats_prev
        for key, (pid, raddr, din, dout) in conns.items():
            prev = self._estats_prev.get(key)
            if prev is None:
                if first_probe:
                    continue      # 第一輪:只立基準,下一輪起算
                # 運行中『新誕生』的連線:累計值就是它本區間的增量,直接起算 ——
                # 否則一檔一關的短 FTP 連線常在拿到基準前就結束,整筆漏排(被算進額度)。
                d_in, d_out = din, dout
            else:
                # 累計值理應單調遞增;若變小代表 4-tuple 被新連線重用 → 用新值當增量(從 0 起算)。
                d_in = din - prev[0] if din >= prev[0] else din
                d_out = dout - prev[1] if dout >= prev[1] else dout
            if d_in < 0:
                d_in = 0
            if d_out < 0:
                d_out = 0
            if d_in or d_out:
                is_lan = self._is_lan_ip(raddr)
                # 暴增定義的是「計入額度」的外網量，來源明細同樣排除內網連線。
                if not is_lan:
                    source_key = (pid, raddr)
                    source_net[source_key] = source_net.get(source_key, 0) + d_in + d_out
                if is_lan:
                    lan_recv += d_in
                    lan_sent += d_out
                else:
                    ext_recv += d_in
                    ext_sent += d_out

        self._estats_prev = {k: (v[2], v[3]) for k, v in conns.items()}
        with self.lock:
            self._pending_lan_sent += lan_sent
            self._pending_lan_recv += lan_recv
            self._pending_ext_sent += ext_sent
            self._pending_ext_recv += ext_recv
            for (pid, raddr), b in source_net.items():
                rec = self._pending_source_net.setdefault(
                    (pid, raddr), [self._proc_name(pid), 0])
                rec[1] += b

    def _proc_name(self, pid) -> str:
        """pid → 程序名(快取;查不到回 pid 字串)。供暴增歸因顯示用。"""
        name = self._name_cache.get(pid)
        if name is None:
            try:
                name = psutil.Process(pid).name()
            except Exception:
                name = f"pid {pid}"
            self._name_cache[pid] = name
        return name

    def _is_lan_ip(self, ip) -> bool:
        """對方位址是否視為「內網、要排除」。count_ranges(強制計入)優先:落在其中一律
        當成『非內網』回 False,確保對外/額度流量(如 GPSS 10.65.x.x)永不被排除。"""
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if any(addr in net for net in self.count_nets):
            return False
        return any(addr in net for net in self.lan_nets)

    def _attribute_surge(self, source_net):
        """以本區間各 IP／程序來源的 ESTATS 網路增量排名，供暴增紀錄逐列顯示。"""
        deltas = sorted(
            ((b, name, pid, raddr)
             for (pid, raddr), (name, b) in source_net.items() if b > 0),
            reverse=True)
        return [{"ip": raddr, "name": name, "pid": pid, "bytes": int(b)}
                for b, name, pid, raddr in deltas]

    def _detect_surge(self, step_total, step_sent, step_recv, dt,
                      source_net, now):
        """在鎖內呼叫。把本次取樣用量入回看視窗;達門檻即歸因並記事件。
        source_net = 本 NIC 區間各 IP／程序來源的 ESTATS 網路增量，供逐來源紀錄。
        門檻 = max(絕對門檻, 近期中位數×倍率);中位數會隨持續下載升高、加冷卻 → 不洗版。"""
        self.byte_hist.append(step_total)
        if not self.surge_enabled:
            return
        hist = list(self.byte_hist)[:-1]          # 不含本筆,當作基準母體
        if len(hist) < 5:                          # 樣本不足(剛啟動),先暖機
            return
        s = sorted(hist)
        n = len(s)
        median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
        threshold = max(self.surge_min_bytes, median * self.surge_factor)
        is_surge = step_total >= threshold
        cooling = (now - self._last_surge_at) < self.surge_cooldown
        if is_surge and not cooling:
            self._last_surge_at = now
            self._surge_seq += 1
            self.surges.append({
                "seq": self._surge_seq,
                "time": datetime.now().strftime("%H:%M:%S"),
                "bytes": int(step_total),
                "recv": int(step_recv),
                "sent": int(step_sent),
                "interval_sec": round(dt, 1),
                "baseline": int(median),
                "top": self._attribute_surge(source_net),
            })

    def snapshot(self) -> dict:
        with self.lock:
            used = self.day_sent + self.day_recv
            remaining = max(0, self.quota_bytes - used)
            peak_down = max((r for _, r in self.rate_hist), default=0.0)
            return {
                "date": self.date,
                "used_bytes": used,
                "sent_bytes": self.day_sent,
                "recv_bytes": self.day_recv,
                "quota_bytes": self.quota_bytes,
                "quota_gb": self.cfg["quota_gb"],
                "remaining_bytes": remaining,
                "percent": (used / self.quota_bytes * 100) if self.quota_bytes else 0,
                "rate_up": self.rate_up,
                "rate_down": self.rate_down,
                "peak_down_window": peak_down,                 # 近 N 分最高下載速率(bytes/s)
                "peak_window_min": self.peak_window_sec / 60,
                "per_nic": {
                    k: {"sent": v[0], "recv": v[1]} for k, v in self.per_nic.items()
                },
                # 「最後更新」誠實反映上次成功取樣的時間,而非當下時間 —— 取樣若卡住,
                # 這個時間就不再前進,頁面也據此判斷資料是否停更。
                "updated_at": self.last_sample_at.strftime("%H:%M:%S")
                              if self.last_sample_at else "—",
                "data_age_sec": round((datetime.now() - self.last_sample_at).total_seconds(), 1)
                                if self.last_sample_at else None,
                # 超過 3 個取樣週期(至少 3 分鐘)沒成功取樣 = 疑似卡住。
                "data_stale": (
                    self.last_sample_at is not None
                    and (datetime.now() - self.last_sample_at).total_seconds()
                        > max(180.0, float(self.cfg.get("sample_interval_sec", 60)) * 3)
                ),
                # 流量暴增預警:開關、門檻、近期事件(新到舊)。
                "surge_enabled": self.surge_enabled,
                "surge_min_mb": round(self.surge_min_bytes / 1048576),
                "surges": list(self.surges)[::-1],
                # 逐次計費明細(新到舊),供網頁 log 框即時診斷內網排除。
                "sample_log": list(self.sample_log)[::-1],
                # 內網排除:開關 + 今日已排除(內網)位元組 + 是否提權(沒提權時 ESTATS
                # 拿不到 per-connection byte,內網排除/暴增歸因停用,頁面據此提醒)。
                "exclude_lan_enabled": self.exclude_lan_enabled,
                "lan_excluded_bytes": self.day_lan_excluded,
                "estats_ok": self._estats_ok,
            }


def run_speedtest(cfg: dict, floor_mbps=None) -> dict:
    """主動測速:從測速點抓一小段,量實際達到的下載速率(Mbps),判斷是否疑似被限速。

    抓到的位元組也會被流量監控算進當日用量(這是真實流量),故會「用掉一點點額度」。
    為了少耗流量:被限速時(速率低)在 timeout 內就讀到低速天花板即停;快線則秒下完
    speedtest_bytes 即停。計時從連線建立後才起算(排除 DNS/TLS 握手延遲)。
    """
    floor = float(floor_mbps) if floor_mbps is not None else float(cfg.get("speed_floor_mbps", 5.0))
    base = cfg.get("speedtest_url", "https://speed.cloudflare.com/__down")
    nbytes = int(cfg.get("speedtest_bytes", 8 * 1024 * 1024))
    timeout = float(cfg.get("speedtest_timeout", 6))
    url = base + ("&" if "?" in base else "?") + "bytes=" + str(nbytes)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "flow-check-speedtest"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            start = time.monotonic()           # 握手後才起算,量純傳輸速率
            got = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                got += len(chunk)
                if time.monotonic() - start >= timeout:
                    break
        elapsed = max(1e-3, time.monotonic() - start)
        mbps = got * 8 / elapsed / 1e6
        return {
            "ok": True,
            "mbps": round(mbps, 2),
            "bytes": got,
            "seconds": round(elapsed, 2),
            "floor_mbps": floor,
            "throttled": mbps < floor,
        }
    except Exception as e:
        return {"ok": False, "message": str(e), "floor_mbps": floor}


def sampler_loop(monitor: Monitor, interval: int):
    while True:
        try:
            monitor.sample()
        except Exception as e:
            record_error("sample", e)
        time.sleep(max(5, interval))


def probe_loop(monitor: Monitor, interval: int):
    """高頻 ESTATS 探測迴圈(與 60s NIC 計費解耦):每 interval 秒取一次各連線累計收/發 byte、
    算增量並累進 pending。間隔短,短而快的內網下載也能被探到兩次、算出增量並排除。"""
    while True:
        try:
            monitor.probe_lan()
        except Exception as e:
            record_error("probe_lan", e)
        time.sleep(max(1, interval))


# ============================================================
# 控制面板 — GPSS launcher (wraps the existing script)
# ============================================================
class JobRunner:
    """Launches the chosen GPSS query script (api/web) with safety guardrails."""

    def __init__(self, cfg: dict, monitor: Monitor):
        self.cfg = cfg
        self.mon = monitor
        self.lock = threading.Lock()

        self.proc = None
        self.thread = None
        self.stop_flag = threading.Event()
        self.chunk_started_wall = None   # 目前這批 subprocess 的開跑時刻(time.time())
        self._chunk_killed = False       # 本批是否被停止鈕/底線硬殺(決定要不要補記時間)
        self.campaign_started_wall = None  # 整個任務(可能多批)的開跑時刻;供控制台顯示即時執行時間
        self.campaign_elapsed = 0.0        # 任務結束後定格的總執行秒數

        self.running = False
        self.status_text = "idle"
        self.mode = None
        self.method = "api"      # 檢索方式: "api"(官方 API) / "web"(傳統前端)
        self.reserve_bytes = 0
        self.target = 0          # requested count (count mode) / campaign cap
        self.est_done = 0        # estimated queries launched so far
        self.started_at = None
        self.lines = collections.deque(maxlen=300)
        self.limit_detect = True
        self.warning = None      # 可用上限偵測:最新警告(None = 無)

        self.gpss_dir = (BASE / cfg["gpss_dir"]).resolve()
        # 兩種檢索方式各對應一支腳本(同一個 gpss_dir,共用 patents.txt / output)。
        self.scripts = {
            "api": cfg.get("gpss_script", "gpss_api_query.py"),
            "web": cfg.get("gpss_script_web", "gpss_web_query.py"),
        }
        self.gpss_script = self.scripts["api"]   # 啟動時依 method 決定,預設 API
        self.python_cmd = self._resolve_python()

    def _resolve_python(self):
        raw = str(self.cfg.get("gpss_python", "")).strip()
        if raw:
            parts = raw.split()
            try:
                r = subprocess.run(parts + ["--version"], capture_output=True, timeout=10)
                if r.returncode == 0:
                    return parts
            except Exception:
                pass
        return [sys.executable]

    def _log(self, msg: str):
        with self.lock:
            self.lines.append(msg.rstrip("\n"))

    def start(self, mode: str, count, reserve_mb, limit_detect=True, method="api"):
        with self.lock:
            if self.running:
                return False, "已有任務在執行中,請先停止"

        if method not in self.scripts:
            return False, f"未知檢索方式: {method}"

        try:
            reserve_mb = max(0, int(reserve_mb))
        except (TypeError, ValueError):
            reserve_mb = int(self.cfg["reserve_mb"])
        reserve_bytes = reserve_mb * 1024 * 1024

        # 剩餘流量低於保留底線就不開跑 —— 但只有「可用上限偵測」開啟時才擋;
        # 關掉偵測代表使用者要求照跑(即使可能超過網路額度)。
        snap = self.mon.snapshot()
        if limit_detect and snap["remaining_bytes"] <= reserve_bytes:
            return False, (f"目前剩餘流量 {snap['remaining_bytes'] / 1048576:.0f}MB "
                           f"已低於保留底線 {reserve_mb}MB,不開跑")

        max_count = int(self.cfg["max_count_per_run"])
        if mode == "count":
            try:
                count = int(count)
            except (TypeError, ValueError):
                return False, "筆數必須是數字"
            if count <= 0:
                return False, "筆數要大於 0"
            if count > max_count:
                return False, f"筆數 {count} 超過單次上限 {max_count} 筆"
            self.target = count
        elif mode == "budget":
            self.target = int(self.cfg.get("max_queries_per_campaign", 0))
        else:
            return False, f"未知模式: {mode}"

        self.stop_flag.clear()
        self.mode = mode
        self.method = method
        self.gpss_script = self.scripts[method]
        self.reserve_bytes = reserve_bytes
        self.limit_detect = bool(limit_detect)
        self.warning = None
        self.est_done = 0
        self.started_at = datetime.now().strftime("%H:%M:%S")
        self.campaign_started_wall = time.time()
        self.campaign_elapsed = 0.0
        self.running = True
        self.status_text = "running"
        with self.lock:
            self.lines.clear()
        self.thread = threading.Thread(target=self._campaign, daemon=True)
        self.thread.start()
        return True, "已開始"

    def _campaign(self):
        max_count = int(self.cfg["max_count_per_run"])
        cap_total = int(self.cfg.get("max_queries_per_campaign", 0))  # 0 = no cap
        try:
            if self.mode == "count":
                self._run_chunk(self.target, offset=0)
                self.est_done = self.target
            else:  # budget: keep running chunks until floor / cap / stop
                offset = 0
                while not self.stop_flag.is_set():
                    snap = self.mon.snapshot()
                    # 同 start():只有開啟「可用上限偵測」時才因保留底線自動停。
                    if self.limit_detect and snap["remaining_bytes"] <= self.reserve_bytes:
                        self._log("--- 剩餘流量已達保留底線,停止 ---")
                        break
                    if cap_total and self.est_done >= cap_total:
                        self._log(f"--- 已達單日上限 {cap_total} 筆,停止 ---")
                        break
                    chunk = max_count
                    if cap_total:
                        chunk = min(chunk, cap_total - self.est_done)
                    self._run_chunk(chunk, offset=offset)
                    self.est_done += chunk
                    offset += chunk
        except Exception as e:
            self._log(f"[error] campaign 例外: {e}")
        finally:
            if self.campaign_started_wall:
                self.campaign_elapsed = time.time() - self.campaign_started_wall
            self.running = False
            self.status_text = "stopped" if self.stop_flag.is_set() else "done"
            self._log(f"=== 任務結束 ({self.status_text}) ===")
            # 任務一結束就立刻轉「完成/停止」並讓子程序退出(上面已 running=False);
            # 把這輪產生的 CSV 併進當月 Excel 丟到背景慢慢做,絕不拖住任務狀態。
            consolidate_async(self.gpss_dir / "output")

    def _run_chunk(self, count: int, offset: int):
        if count <= 0:
            return
        cmd = self.python_cmd + [self.gpss_script, str(count), str(offset)]
        self._log(f"$ {' '.join(cmd)}   (cwd={self.gpss_dir})")
        # Force the child to emit UTF-8 so its Chinese output doesn't arrive as
        # cp950 mojibake in the web log (we decode the pipe as UTF-8 below).
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        # 不要緩衝子程序的 stdout,否則逐筆的 [idx/總數] print 會卡在管道緩衝裡,
        # 拖到累積約 8KB 或整個跑完才一次冒出來 → 網頁 log 看起來「沒有即時更新」。
        env["PYTHONUNBUFFERED"] = "1"
        # 子程序也別在同步夾裡寫 .pyc(同上,避免 __pycache__ 跨機衝突/拖慢同步)。
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["GPSS_LIMIT_DETECT"] = "1" if getattr(self, "limit_detect", True) else "0"
        # 併檔(零散 CSV → 當月 Excel)改由伺服器在任務結束後背景做,子程序不要碰 ——
        # 否則它印完 summary 還卡在 Google Drive 上慢吞吞的 load/save,任務遲遲不結束、
        # 網頁不會自動停。子程序看到這個旗標就略過自己的 consolidate,印完 summary 即退出。
        env["GPSS_SKIP_CONSOLIDATE"] = "1"
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.gpss_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
            )
        except Exception as e:
            self._log(f"[error] 無法啟動程式: {e}")
            return

        with self.lock:
            self.proc = proc
            self.chunk_started_wall = time.time()
            self._chunk_killed = False

        def supervise():
            while proc.poll() is None:
                if self.stop_flag.is_set():
                    self._terminate(proc)
                    return
                # 同 start():關閉「可用上限偵測」時不因保留底線中止執行中的批次。
                snap = self.mon.snapshot()
                if self.limit_detect and snap["remaining_bytes"] <= self.reserve_bytes:
                    self._log("--- 觸及流量保留底線,中止本批 ---")
                    self.stop_flag.set()
                    self._terminate(proc)
                    return
                time.sleep(2)

        sup = threading.Thread(target=supervise, daemon=True)
        sup.start()

        try:
            for line in proc.stdout:
                s = line.rstrip("\n")
                if "##WARN##" in s:
                    msg = s.split("##WARN##", 1)[1].strip()
                    with self.lock:
                        self.warning = msg
                    self._log("⚠ " + msg)
                elif "##CLEAR##" in s:
                    msg = s.split("##CLEAR##", 1)[1].strip()
                    with self.lock:
                        self.warning = None
                    self._log("✓ " + msg)
                else:
                    self._log(s)
        except Exception as e:
            self._log(f"[warn] 讀取輸出中斷: {e}")
        proc.wait()
        sup.join(timeout=3)

        started, killed = self.chunk_started_wall, self._chunk_killed
        with self.lock:
            self.proc = None
            self.chunk_started_wall = None
        if killed and started:
            # 子程式被硬殺,來不及自己寫 run_log → 由父程式補記本批執行時間,
            # 否則被中止那批的執行時間會整段漏掉。
            self._record_elapsed(time.time() - started)

    def _record_elapsed(self, elapsed: float):
        """補記一筆執行時間到本機 run_log(僅在子程式被中止、自己沒能寫時用)。
        筆數/流量另由 CSV 權威統計,不在此記,避免與 CSV 重複。"""
        if elapsed <= 0:
            return
        try:
            now = datetime.now()
            rec = {
                "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                "date": now.strftime("%Y-%m-%d"),
                "elapsed_sec": round(elapsed, 1),
                "machine": MACHINE_ID,
                "note": "stopped-partial",
            }
            path = RUN_LOG_PATH
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as lf:
                lf.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            self._log(f"[warn] 補記執行時間失敗: {e}")

    def _terminate(self, proc):
        self._chunk_killed = True
        try:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass

    def stop(self):
        self.stop_flag.set()
        with self.lock:
            p = self.proc
        if p is not None:
            self._terminate(p)
        return True, "停止指令已送出(會在目前批次邊界安全停下)"

    def clear_warning(self):
        """使用者在網頁按掉警告橫幅時呼叫:清掉目前這則警告(伺服器端的 self.warning)。
        否則撞額度等警告會留在 status() 裡,每次輪詢都回傳 → 一重整網頁又跳回來。
        清掉後重整/重開都不再出現;之後新的一輪若又偵測到(如再次撞額度)會再設新的。"""
        with self.lock:
            self.warning = None
        return True

    def live_elapsed_today(self) -> float:
        """目前這一批正在跑、還沒寫進 run_log 的執行秒數(僅當這批啟動於今天)。
        爬取統計的執行時間平時來自 run_log(每批跑完才追加),這個補上「跑到一半」
        那段,讓統計頁的執行時間在執行中也會即時累加。"""
        with self.lock:
            start = self.chunk_started_wall
            running = self.running
        if not running or not start:
            return 0.0
        if datetime.fromtimestamp(start).strftime("%Y-%m-%d") != datetime.now().strftime("%Y-%m-%d"):
            return 0.0  # 跨日的就不硬算
        return max(0.0, time.time() - start)

    def status(self) -> dict:
        with self.lock:
            # 執行時間:跑的時候即時算(now - 開跑時刻),結束後定格在 campaign_elapsed。
            if self.running and self.campaign_started_wall:
                elapsed = time.time() - self.campaign_started_wall
            else:
                elapsed = self.campaign_elapsed
            return {
                "running": self.running,
                "status": self.status_text,
                "mode": self.mode,
                "method": self.method,
                "started_at": self.started_at,
                "elapsed_sec": round(elapsed, 1),
                "target": self.target,
                "est_done": self.est_done,
                "warning": self.warning,
                "lines": list(self.lines)[-50:],
            }


# ============================================================
# Web server
# ============================================================
class Handler(BaseHTTPRequestHandler):
    monitor: Monitor = None
    runner: JobRunner = None
    config: dict = None

    def _send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        if self.path.startswith("/api/status"):
            snap = self.monitor.snapshot()
            # mtime 當快篩(沒檔變新就免讀檔),變新才比對內容雜湊確認:
            # Google Drive 同步常動 mtime 但內容沒變,只看 mtime 會誤報「需重啟」。
            snap["needs_restart"] = _needs_restart()
            with _HEALTH_LOCK:
                snap["server_errors"] = _HEALTH["count"]
                snap["recent_errors"] = list(_HEALTH["errors"])[-5:]
            # 檢索資料的變動簽章:前端只在它變了才刷新爬取統計(避免每 5 秒盲刷)。
            snap["data_sig"] = _data_sig(self.config)
            self._send_json(snap)
        elif self.path.startswith("/api/job"):
            self._send_json(self.runner.status())
        elif self.path.startswith("/api/config"):
            cfg = self.config
            api_set, patdb = False, ""
            # 本機 API 設定優先(api_config_<機器>.json),沒有才看共用的 api_config.json。
            ac = {}
            for name in ("api_config.json", f"api_config_{MACHINE_ID}.json"):
                apath = gpss_path(cfg, name)
                if apath.exists():
                    try:
                        ac.update(json.loads(apath.read_text(encoding="utf-8")))
                    except Exception:
                        pass
            api_set = bool(str(ac.get("userCode", "")).strip())
            patdb = ac.get("patDB", "")
            # 本機 UI 偏好(用量通知);未設過回 None,讓前端沿用當下狀態並回寫種子。
            prefs = load_prefs()
            self._send_json({
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
            })
        elif self.path.startswith("/api/analytics"):
            self._send_json(analytics_payload(self.config, self.monitor, self.runner))
        elif self.path.startswith("/api/monthly/trend"):
            # 達標分析趨勢:只讀取本功能啟用後、使用者主動更新所留下的快照,不回推舊資料。
            if monthly_progress is None:
                self._send_json({"ok": False, "points": [], "snapshots": [], "months": [],
                                 "message": "monthly_progress 模組載入失敗"})
            else:
                try:
                    query = self.path.split("?", 1)[1] if "?" in self.path else ""
                    match = re.search(r"(?:^|&)month=(\d{4}-\d{2})(?:&|$)", query)
                    self._send_json({"ok": True, **monthly_progress.trend(match.group(1) if match else None)})
                except Exception as e:
                    self._send_json({"ok": False, "points": [], "snapshots": [], "months": [],
                                     "message": f"讀取趨勢失敗:{e}"})
        elif self.path.startswith("/api/monthly"):
            # 月進度:讀 data/ 下手動下載的後台匯出檔(簡易版 + API),加總檢索次數。
            if monthly_progress is None:
                self._send_json({"ok": False, "files_found": False,
                                 "message": "monthly_progress 模組載入失敗"})
            else:
                try:
                    self._send_json(monthly_progress.compute())
                except Exception as e:
                    self._send_json({"ok": False, "files_found": False,
                                     "message": f"計算失敗:{e}"})
        elif self.path.startswith("/api/keys"):
            # 驗證碼池(只回遮罩碼,不外洩明碼)+ 本機目前選用哪一組。
            g = _gpss_module()
            if g is None:
                self._send_json({"ok": False, "keys": [], "message": "驗證碼模組載入失敗"})
                return
            try:
                keys = g.load_keys()
                cur = g.resolve_current_key(keys)
                self._send_json({
                    "ok": True,
                    "active": (cur or {}).get("id"),
                    "keys": [{"id": k.get("id"), "label": k.get("label", ""),
                              "masked": g._mask_code(k.get("userCode", ""))} for k in keys],
                })
            except Exception as e:
                self._send_json({"ok": False, "keys": [], "message": str(e)})
        elif self.path.startswith("/api/patents"):
            p = gpss_path(self.config, "patents.txt")
            text = p.read_text(encoding="utf-8") if p.exists() else ""
            count = sum(1 for l in text.splitlines()
                        if l.strip() and not l.strip().startswith("#"))
            self._send_json({"ok": True, "text": text, "count": count})
        elif self.path in ("/", "/index.html"):
            try:
                html = _render_index()
            except FileNotFoundError:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"web/index.html not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            # 不讓瀏覽器快取:否則改了網頁/JS,重整仍可能跑到舊版(新舊混用會出怪事)。
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(html)
        elif self.path.endswith((".css", ".js")):
            # index.html 拆出來的靜態檔(style.css、js/*.js)。只服務 web/ 內、
            # 副檔名限定 .css/.js 的檔;解析後確認沒跑出 web/(擋路徑穿越),
            # 其他一律 404/403。每次請求即時讀檔(改完重整即生效,不必重啟)。
            rel = self.path.lstrip("/").split("?", 1)[0]
            target = (WEB_DIR / rel).resolve()
            try:
                target.relative_to(WEB_DIR.resolve())
            except ValueError:
                self.send_response(403)
                self.end_headers()
                return
            ctype = "text/css" if target.suffix == ".css" else "application/javascript"
            try:
                body = target.read_bytes()
            except (FileNotFoundError, IsADirectoryError):
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # 同上:JS/CSS 不快取,改完重整一定跑到新版(配合「每次請求即時讀檔」)。
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path.startswith("/api/monthly/refresh"):
            # 只有使用者明確按「更新計算」才留下達標趨勢快照;讀取/月統計輪詢一律不寫。
            if monthly_progress is None:
                self._send_json({"ok": False, "files_found": False,
                                 "message": "monthly_progress 模組載入失敗"}, code=500)
            else:
                try:
                    progress = monthly_progress.compute()
                    snap = monthly_progress.record_snapshot(progress)
                    self._send_json({**progress, "snapshot": snap,
                                     "trend": monthly_progress.trend(progress.get("month"))})
                except Exception as e:
                    self._send_json({"ok": False, "files_found": False,
                                     "message": f"更新計算失敗:{e}"}, code=500)
        elif self.path.startswith("/api/run"):
            body = self._read_body()
            mode = body.get("mode", "count")
            count = body.get("count")
            reserve_mb = body.get("reserve_mb", self.config["reserve_mb"])
            limit_detect = body.get("limit_detect", True)
            method = body.get("method", "api")
            ok, msg = self.runner.start(mode, count, reserve_mb, limit_detect, method)
            self._send_json({"ok": ok, "message": msg}, code=200 if ok else 409)
        elif self.path.startswith("/api/prefs"):
            # 網頁 UI 偏好(用量通知開關/門檻)寫回本機偏好檔,重開即還原(各台獨立)。
            body = self._read_body()
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
                self.monitor.surge_enabled = patch["surge_enabled"]
            if "surge_min_mb" in body:
                try:
                    mb = max(1, int(body["surge_min_mb"]))
                    patch["surge_min_mb"] = mb
                    self.monitor.surge_min_bytes = mb * 1024 * 1024
                except (TypeError, ValueError):
                    pass
            if "exclude_lan" in body:
                patch["exclude_lan"] = bool(body["exclude_lan"])
                self.monitor.exclude_lan_enabled = patch["exclude_lan"]
            try:
                prefs = save_prefs(patch) if patch else load_prefs()
                self._send_json({"ok": True, "prefs": prefs})
            except Exception as e:
                record_error("save_prefs", e)
                self._send_json({"ok": False, "message": str(e)}, code=500)
        elif self.path.startswith("/api/keys"):
            # action="add":新增一組到驗證碼池;否則(帶 id)= 切換本機選用的驗證碼。
            body = self._read_body()
            g = _gpss_module()
            if g is None:
                self._send_json({"ok": False, "message": "驗證碼模組載入失敗"}, code=500)
                return
            try:
                if body.get("action") == "add":
                    label = (body.get("label") or "").strip()
                    uc = (body.get("userCode") or "").strip()
                    if not uc:
                        self._send_json({"ok": False, "message": "請輸入驗證碼"}, code=400)
                        return
                    keys = g.load_keys()
                    dup = next((k for k in keys if k.get("userCode") == uc), None)
                    if dup:
                        self._send_json({"ok": False, "message":
                                         f"此驗證碼已在池中:[{dup.get('id')}] {dup.get('label','')}"},
                                        code=409)
                        return
                    used = {str(k.get("id", "")).upper() for k in keys}
                    nid = next((chr(c) for c in range(ord("A"), ord("Z") + 1)
                                if chr(c) not in used), f"K{len(keys)}")
                    nk = {"id": nid, "label": label or nid, "userCode": uc}
                    keys.append(nk)
                    g.save_keys(keys)
                    self._send_json({"ok": True, "id": nid, "label": nk["label"],
                                     "masked": g._mask_code(uc)})
                    return
                kid = body.get("id")
                keys = g.load_keys()
                match = next((k for k in keys if k.get("id") == kid), None)
                if not match:
                    self._send_json({"ok": False, "message": f"找不到驗證碼 {kid}"}, code=404)
                    return
                g.write_active_key(match)
                self._send_json({"ok": True, "active": match.get("id"),
                                 "label": match.get("label", ""),
                                 "masked": g._mask_code(match.get("userCode", ""))})
            except Exception as e:
                self._send_json({"ok": False, "message": str(e)}, code=500)
        elif self.path.startswith("/api/clearwarn"):
            # 使用者按掉警告橫幅:清掉伺服器端的警告,避免重整網頁後又跳回來。
            self.runner.clear_warning()
            self._send_json({"ok": True})
        elif self.path.startswith("/api/stop"):
            ok, msg = self.runner.stop()
            self._send_json({"ok": ok, "message": msg})
        elif self.path.startswith("/api/speedtest"):
            # 主動測速:抓一小段量實際速率,判斷是否疑似被限速。前端可帶 floor_mbps 覆蓋門檻。
            body = self._read_body()
            self._send_json(run_speedtest(self.config, body.get("floor_mbps")))
        elif self.path.startswith("/api/patents"):
            body = self._read_body()
            text = body.get("text", "")
            p = gpss_path(self.config, "patents.txt")
            try:
                p.write_text(text, encoding="utf-8")
                count = sum(1 for l in text.splitlines()
                            if l.strip() and not l.strip().startswith("#"))
                self._send_json({"ok": True, "count": count,
                                 "message": f"已儲存 {count} 筆"})
            except Exception as e:
                self._send_json({"ok": False, "message": str(e)}, code=500)
        else:
            self.send_response(404)
            self.end_headers()

    def handle(self):
        # 瀏覽器切分頁/重新整理/關頁時會中途斷線(WinError 10053/10054),對這種
        # 每幾秒輪詢一次的頁面很常見、也無害 —— 吞掉,別讓主控台噴一整串 traceback。
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass  # 瀏覽器中途斷線,無害,不算錯誤
        except Exception as e:
            record_error("http", e)  # 真正的處理錯誤:記下來讓頁面看得到

    def log_message(self, *args):
        pass  # keep the console clean


def _ensure_machines_file():
    """確保 data/config/machines.json 存在,並把本機代號加進去(值留空 = 顯示時用代號)。
    妳只要把值改成「桌機」「筆電」之類,各機統計欄位就會顯示友善名稱。"""
    MACHINES_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if MACHINES_PATH.exists():
        try:
            data = json.loads(MACHINES_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if MACHINE_ID not in data:
        data[MACHINE_ID] = ""   # 留空 → machine_label 會退回顯示代號
        try:
            MACHINES_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass


def main():
    cfg = load_config()
    _ensure_machines_file()

    port = int(cfg["port"])
    url = f"http://127.0.0.1:{port}"
    # 先綁定連接埠,把它當「同一台機器只准跑一個」的鎖。綁不到 = 已經有一個在跑,
    # 立刻退出 —— 重點是「在啟動取樣器之前」就擋掉,否則第二個實例會跟著每隔
    # sample_interval 寫一次 state_<機器>.json,在 Google Drive 上不斷產生衝突複本
    # (state_<機器> (1).json…),Excel 也會被兩個實例搶著 consolidate 而生衝突複本,
    # 進而讓統計把衝突複本當成「多一台機器」重複計數。
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        print(f"[info] 連接埠 {port} 已被占用 —— 應該已經有一個在背景跑了。")
        print(f"       直接開瀏覽器看:{url}")
        # 已經有實例在跑、正在服務 → 直接開瀏覽器連它(瀏覽器由本程式開,
        # 不在 .bat 裡先開,避免搶在伺服器綁定連接埠前打開、出現「無法連上 127.0.0.1」)。
        webbrowser.open(url)
        return

    monitor = Monitor(cfg)
    runner = JobRunner(cfg, monitor)

    Handler.monitor = monitor
    Handler.runner = runner
    Handler.config = cfg

    t = threading.Thread(
        target=sampler_loop, args=(monitor, int(cfg["sample_interval_sec"])), daemon=True
    )
    t.start()

    # 高頻 ESTATS 探測迴圈:跟上面 60s 的 NIC 計費分開跑,負責內網排除/暴增歸因的網路增量。
    probe_int = max(1, int(cfg.get("lan_probe_interval_sec", 1)))
    tp = threading.Thread(target=probe_loop, args=(monitor, probe_int), daemon=True)
    tp.start()

    # 開機後第一次打開工具,爬取統計頁常要等很久:此時行內快取(run_log / OK 列)都是空的、
    # _daily_cache 雖在但當月 Excel 要重算,而檔案又在 Google Drive 同步夾裡 —— 剛開機 Drive
    # 多半還在「線上即取」狀態,第一次讀每個月份檔都要即時下載,全卡在使用者點開那一刻的轉圈上。
    # 對策:伺服器一啟動就在背景先跑一次 analytics_payload 把所有快取(daily_cache、_RUNLOG_CACHE、
    # _OK_ROWS_CACHE)和 Drive 檔案熱起來。預設分頁是流量監控,使用者通常過幾秒才點到爬取統計,
    # 這段時間正好讓背景把重活做完;真的點得很快也只是退回原本的等待,不會更慢。純加速,壞了不影響正確性。
    def _prewarm_analytics():
        try:
            analytics_payload(cfg, monitor, runner)
        except Exception:
            pass
    threading.Thread(target=_prewarm_analytics, daemon=True).start()

    print("=" * 56)
    print(f"  流量監控啟動 (v{VERSION}) → {url}")
    print(f"  每日額度 {cfg['quota_gb']}GB  |  取樣間隔 {cfg['sample_interval_sec']}s"
          f"  |  連線探測 {probe_int}s")
    print(f"  本機代號: {MACHINE_ID}  (顯示名:{machine_label(MACHINE_ID)})")
    print(f"  GPSS 直譯器: {' '.join(runner.python_cmd)}")
    if monitor._estats_ok:
        print("  內網排除: 已啟用(管理員身分,ESTATS 取每條連線收/發 byte)")
    else:
        print("  [warn] 非管理員身分啟動 —— 內網排除/暴增歸因停用(ESTATS 需提權)。")
        print("         要排除內網(FileZilla 等)流量,請用 start_monitor.bat 以管理員身分啟動。")
    print("  關閉:在此視窗按 Ctrl+C")
    print("=" * 56)

    # 連接埠此時已綁定並開始 listen(ThreadingHTTPServer 建構時就 bind+listen),
    # 此刻才開瀏覽器,第一個請求一定連得上 —— 不會再出現「無法連上 127.0.0.1」、
    # 等重整才載得出來。瀏覽器由本程式開(不在 .bat 裡先開),消除 .bat 搶先打開
    # 卻在伺服器尚未就緒時連線失敗的競態(重新連上網路、Google Drive 正忙著同步、
    # 讀同步夾裡的程式/網頁檔變慢時,這個競態視窗會被拉大、特別容易發生)。
    webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止。")
        httpd.shutdown()


if __name__ == "__main__":
    main()
