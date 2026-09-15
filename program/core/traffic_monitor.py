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

# 把 program/ 放上 sys.path，讓 core / gpss / common / yearly_stats 都能以套件匯入。
# 這是全專案唯一動 sys.path 的地方（每個進入點各一行），取代原本散在模組內的路徑魔法。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from core import monthly_progress   # 月進度計算(讀 data/ 下手動下載的後台匯出檔)
except Exception:
    monthly_progress = None


from common.jsonio import read_json, write_json_atomic
# Windows ESTATS(每條 TCP 連線的精準收/發位元組)已抽到獨立模組 estats.py。
# 對外只用到這兩個函式:process_is_elevated() 判是否有提權、estats_probe() 取逐連線位元組。
from core.estats import process_is_elevated, estats_probe
# 錯誤收集與「需重啟」偵測抽到 health.py(只相依標準函式庫,最底層)。
from core.health import record_error, health_snapshot, needs_restart
# 路徑/機器身分常數抽到 paths.py;資料層(設定/偏好/機器標籤/output 統計掃描)抽到 storage.py。
# 兩者皆為單向相依的下層模組(本檔 → paths/storage),不會回頭匯入本檔,避免循環匯入。
from core.paths import (BASE, DATA_DIR, MACHINE_DATA_DIR, MACHINES_PATH, WEB_DIR, MACHINE_ID,
                        RUN_LOG_PATH, STATE_PATH)
from core.storage import (output_store, load_prefs, save_prefs, machine_label,
                          load_config, gpss_path, _gpss_module,
                          scan_output_daily, load_run_elapsed)
# API 額度 / 驗證碼歸戶抽到 quota.py(相依 paths + storage,不回頭匯入本檔)。
from core.quota import api_quota_status, api_case_hourly
# 流量取樣與計費(Monitor 類別、兩個背景迴圈、主動測速)抽到 sampler.py。
# 相依 health/estats/paths/storage,不回頭匯入本檔。
from core.sampler import Monitor, sampler_loop, probe_loop, run_speedtest
# 檢索任務調度(JobRunner 與任務結束後的 Excel 彙整 consolidate_async)抽到 jobs.py。
# 相依 health/paths/sampler/storage,不回頭匯入本檔。
from core.jobs import JobRunner


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
            snap["needs_restart"] = needs_restart()
            h = health_snapshot()
            snap["server_errors"] = h["count"]
            snap["recent_errors"] = h["errors"]
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
                    one = read_json(apath, default={})
                    if isinstance(one, dict):
                        ac.update(one)
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
    data = read_json(MACHINES_PATH, default={})
    if not isinstance(data, dict):
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
