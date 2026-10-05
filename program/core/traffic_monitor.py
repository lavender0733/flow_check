"""
Traffic Monitor
===============

A small always-local web app: 流量監控 — reads cumulative NIC byte counters
(all adapters summed) via psutil, accumulates the day's usage against a daily
quota (default 2GB up+down), and serves it as a live web page, together with
the 流量報表 (daily usage history).

Why read cumulative counters (not sampled rate): the counter is monotonic,
so "current - last" is exact regardless of how often we poll — no traffic
is missed between samples. Reboots reset the counter; we detect that
(current < last) and treat the current value as the post-reset delta.

這個檔現在只做組裝:載設定 → 造 Monitor → 起兩個背景迴圈 → 開伺服器。
實作分散在單向相依的下層模組:health → sampler → routes,
HTTP 管線在 common/webapp.py。

Run:   python program/core/traffic_monitor.py   →   http://127.0.0.1:8765
Stop:  Ctrl+C in this window.
"""

import json
import socket
import sys
import threading
import webbrowser
from pathlib import Path

# 別把 .pyc 寫進這個資料夾 —— 它在 Google Drive 同步夾裡,兩台機器(且 Python 版本不同)
# 各自產生的 __pycache__/*.pyc 會互相同步、產生衝突複本,白白佔用同步頻寬、拖慢真資料同步。
# 須在匯入其他模組之前設定,才能擋掉它們的 .pyc。
sys.dont_write_bytecode = True

# 把 program/ 放上 sys.path，讓 core / common 都能以套件匯入。
# 這是全專案唯一動 sys.path 的地方（每個進入點各一行），取代原本散在模組內的路徑魔法。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.jsonio import read_json
from common.webapp import serve
# 錯誤收集抽到 health.py(只相依標準函式庫,最底層)。
from core.health import record_error
from core.paths import MACHINES_PATH, MACHINE_ID, WEB_DIR
# HTTP 端點與路由表抽到 routes.py;它相依 health/sampler/storage,
# 不回頭匯入本檔(VERSION 以參數傳給 render_index),避免循環匯入。
from core.routes import App, build_routes, render_index
from core.sampler import Monitor, probe_loop, sampler_loop
from core.storage import load_config, machine_label


# 系統版本(語意化版本控制)。這是唯一來源:網頁底部的「系統版本」會由
# render_index() 把 index.html 裡的 <!--#version--> 換成這個值,改版只改這裡。
VERSION = "8.1.0"


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


def _start_xfer690():
    """載入 690 資轉監控並起背景抓信。回傳 (服務或 None, banner 用的一行說明)。

    program/xfer690 不存在(TIPO flow check)時安靜略過;載入失敗只記錯,流量監控照常啟動。
    """
    if not (Path(__file__).resolve().parents[1] / "xfer690").is_dir():
        return None, None
    try:
        from xfer690.service import Xfer690Service
        xfer = Xfer690Service()
    except Exception as e:
        record_error("xfer690", e)
        return None, f"  690 資轉監控: 載入失敗({e})"
    if not xfer.cfg.get("enabled", True):
        return xfer, "  690 資轉監控: 停用(data/config/xfer690.json 的 enabled)"
    threading.Thread(target=xfer.loop, daemon=True).start()
    return xfer, f"  690 資轉監控: 每 {xfer.cfg['poll_interval_min']} 分鐘抓信"


def _startup_banner(cfg, monitor, url, probe_int, xfer_line=None) -> str:
    """主控台啟動訊息。回傳字串交給 serve() 印(它會在綁好連接埠、開瀏覽器前印出)。"""
    lines = [
        "=" * 56,
        f"  流量監控啟動 (v{VERSION}) → {url}",
        f"  每日額度 {cfg['quota_gb']}GB  |  取樣間隔 {cfg['sample_interval_sec']}s"
        f"  |  連線探測 {probe_int}s",
        f"  本機代號: {MACHINE_ID}  (顯示名:{machine_label(MACHINE_ID)})",
    ]
    if xfer_line:
        lines.append(xfer_line)
    if monitor._estats_ok:
        lines.append("  內網排除: 已啟用(管理員身分,ESTATS 取每條連線收/發 byte)")
    else:
        lines.append("  [warn] 非管理員身分啟動 —— 內網排除/暴增歸因停用(ESTATS 需提權)。")
        lines.append("         要排除內網(FileZilla 等)流量,請用 start_monitor.bat 以管理員身分啟動。")
    lines.append("  關閉:在此視窗按 Ctrl+C")
    lines.append("=" * 56)
    return "\n".join(lines)


def main():
    cfg = load_config()
    _ensure_machines_file()

    port = int(cfg["port"])
    url = f"http://127.0.0.1:{port}"
    # 先試綁連接埠,把它當「同一台機器只准跑一個」的鎖。綁不到 = 已經有一個在跑,
    # 立刻退出 —— 重點是「在啟動取樣器之前」就擋掉,否則第二個實例會跟著每隔
    # sample_interval 寫一次 state_<機器>.json,在 Google Drive 上不斷產生衝突複本
    # (state_<機器> (1).json…)。
    #
    # 真正的綁定在 serve() 內部,所以這裡用一個臨時 socket 探測。探測與真正綁定之間
    # 有一個極短的競態窗口(兩個實例同時探測都成功);實務上使用者是雙擊 .bat 啟動,
    # 不會在同一毫秒內啟動兩次,而真正綁定失敗時 serve() 會拋 OSError,主控台仍看得到。
    try:
        probe = socket.socket()
        probe.bind(("127.0.0.1", port))
        probe.close()
    except OSError:
        print(f"[info] 連接埠 {port} 已被占用 —— 應該已經有一個在背景跑了。")
        print(f"       直接開瀏覽器看:{url}")
        # 已經有實例在跑、正在服務 → 直接開瀏覽器連它(瀏覽器由本程式開,
        # 不在 .bat 裡先開,避免搶在伺服器綁定連接埠前打開、出現「無法連上 127.0.0.1」)。
        webbrowser.open(url)
        return

    monitor = Monitor(cfg)
    xfer, xfer_line = _start_xfer690()
    app = App(cfg, monitor, xfer)

    threading.Thread(target=sampler_loop,
                     args=(monitor, int(cfg["sample_interval_sec"])),
                     daemon=True).start()

    # 高頻 ESTATS 探測迴圈:跟上面 60s 的 NIC 計費分開跑,負責內網排除/暴增歸因的網路增量。
    probe_int = max(1, int(cfg.get("lan_probe_interval_sec", 1)))
    threading.Thread(target=probe_loop, args=(monitor, probe_int), daemon=True).start()

    # serve() 先綁定再開瀏覽器,第一個請求一定連得上 —— 不會再出現「無法連上 127.0.0.1」、
    # 等重整才載得出來。瀏覽器由本程式開(不在 .bat 裡先開),消除 .bat 搶先打開卻在伺服器
    # 尚未就緒時連線失敗的競態(重新連上網路、Google Drive 正忙著同步、讀同步夾裡的
    # 程式/網頁檔變慢時,這個競態視窗會被拉大、特別容易發生)。
    serve(
        port,
        build_routes(app),
        static_dir=WEB_DIR,
        index_renderer=lambda: render_index(VERSION),
        on_error=record_error,
        banner=_startup_banner(cfg, monitor, url, probe_int, xfer_line),
    )


if __name__ == "__main__":
    main()
