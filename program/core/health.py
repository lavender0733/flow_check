"""伺服器健康狀態與「需重啟」偵測。

從 traffic_monitor.py 抽出的下層模組（只相依標準函式庫）。

兩件事：
1. 把原本只 print 到主控台的錯誤收進來，讓網頁直接看得到（錯誤累積或資料
   停更時頁面會提醒重啟），使用者不必再去翻 cmd 視窗。
2. 偵測「磁碟上的程式碼已更新，但這個執行中的實例還是舊版」—— Python 模組
   在啟動時載入，改了要重啟才生效。
"""

import collections
import hashlib
import threading
from datetime import datetime
from pathlib import Path


# 監看「在這個程序內執行」的原始碼檔:啟動時記下狀態,之後若磁碟上的檔內容變了,
# 代表妳改了程式、但這個正在跑的實例還是舊版 → 前端顯示「需重啟」。
_CORE_DIR = Path(__file__).resolve().parent


def _watched_sources() -> list:
    """在這個程序內載入、改了需要重啟才生效的原始碼檔。

    掃 core/*.py 而非硬編清單：拆檔或新增模組時會自動納入，不會漏掉。
    另含 gpss/output_store.py（統計與額度都在這個程序內用它）與 common/*.py。

    不列入：web/index.html 與 partials（每次請求即時讀檔）、
    gpss_*_query.py（每次檢索開新行程），改完重整或下次執行就生效。
    """
    files = sorted(_CORE_DIR.glob("*.py"))
    files += sorted((_CORE_DIR.parent / "common").glob("*.py"))
    files.append(_CORE_DIR.parent / "gpss" / "output_store.py")
    return files


def _source_mtime() -> float:
    newest = 0.0
    for p in _watched_sources():
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
    for p in _watched_sources():
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"\x00<missing:" + str(p).encode("utf-8") + b">\x00")
        h.update(b"\n--\n")
    return h.hexdigest()


_STARTUP_MTIME = _source_mtime()
_STARTUP_HASH = _source_hash()


def needs_restart() -> bool:
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


def health_snapshot() -> dict:
    """供 /api/status 使用的健康狀態快照。

    回傳 {"count": 累計錯誤筆數, "errors": [最近 5 筆訊息]}。
    鎖在這裡收斂，呼叫端不必知道 _HEALTH 的內部結構。
    """
    with _HEALTH_LOCK:
        return {"count": _HEALTH["count"], "errors": list(_HEALTH["errors"])[-5:]}
    
