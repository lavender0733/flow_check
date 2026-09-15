"""檢索任務調度。

從 traffic_monitor.py 抽出。把 GPSS 檢索程式以 subprocess 啟動並套上單輪筆數
上限，提供即時進度與停止鈕。兩種方式："api"（gpss_api_query.py，官方 API）與
"web"（gpss_web_query.py，Playwright 操作前端）。檢索邏輯本身不在這裡，
這層只是包裝。

流量保留底線（剩餘低於 X MB 就不開跑／跑到一半自動停）已於 7.3.0 移除：
檢索是否該停由使用者自己決定，不再由流量多寡代為中斷。

consolidate_async 一併搬來：它唯一的呼叫者就是 JobRunner（檢索結束後把零散
CSV 併成各機一個 Excel）。
"""

import collections
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

from core.health import record_error
from core.paths import BASE, MACHINE_ID, RUN_LOG_PATH
from core.sampler import Monitor
from core.storage import output_store


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
        self.target = 0          # 這次要跑的筆數
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

    def start(self, mode: str, count, limit_detect=True, method="api"):
        with self.lock:
            if self.running:
                return False, "已有任務在執行中,請先停止"

        if method not in self.scripts:
            return False, f"未知檢索方式: {method}"

        max_count = int(self.cfg["max_count_per_run"])
        if mode != "count":
            return False, f"未知模式: {mode}"
        try:
            count = int(count)
        except (TypeError, ValueError):
            return False, "筆數必須是數字"
        if count <= 0:
            return False, "筆數要大於 0"
        if count > max_count:
            return False, f"筆數 {count} 超過單次上限 {max_count} 筆"
        self.target = count

        self.stop_flag.clear()
        self.mode = mode
        self.method = method
        self.gpss_script = self.scripts[method]
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
        try:
            self._run_chunk(self.target, offset=0)
            self.est_done = self.target
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
            # 只等使用者按「立即停止」—— 流量多寡不再中斷檢索。
            while proc.poll() is None:
                if self.stop_flag.is_set():
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
