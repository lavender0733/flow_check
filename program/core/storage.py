"""
資料層:設定 / 偏好 / 機器標籤 / output 統計掃描 / run_log 執行時間
====================================================================
從 traffic_monitor.py 抽出。相依:paths(路徑常數)與 program/gpss/output_store(統計來源)。
不呼叫 traffic_monitor 內的任何東西,維持單向相依(traffic_monitor → storage),避免循環匯入。
"""

import sys
import csv
import json
import re
import threading
from pathlib import Path

from common.jsonio import file_sig as _file_sig, read_json, write_json_atomic
from core.paths import BASE, MACHINE_DATA_DIR, CONFIG_PATH, MACHINES_PATH, PREFS_PATH

# output 彙整模組(在 program/gpss/ 下):把零散 CSV 併成各機一個 Excel,統計也讀它。
try:
    from gpss import output_store
except Exception:
    output_store = None


# ── 網頁 UI 偏好(各機獨立檔)──────────────────────────────────────
# 偏好檔寫入序列化鎖:ThreadingHTTPServer 每個請求一條執行緒,前端載入時會同時 POST 好幾個
# 偏好種子。沒有鎖時多條執行緒同時寫同一個檔,在 Google Drive 的虛擬磁碟上會丟
# [Errno 22] Invalid argument。鎖 + 原子寫(tmp→replace)+ 重試解決。
_PREFS_LOCK = threading.Lock()


def load_prefs() -> dict:
    if not PREFS_PATH.exists():
        return {}
    try:
        text = PREFS_PATH.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Google Drive 同步衝突偶爾會讓這個檔短暫變成「兩段 JSON 黏在一起」(報 Extra data)
        # 或尾端殘留。用 raw_decode 救出開頭那個完整物件、忽略多餘尾段(下次 save_prefs 會寫乾淨),
        # 這樣同步空窗造成的暫時壞檔不會洗版警告、也不會白白丟掉偏好。
        try:
            obj, _ = json.JSONDecoder().raw_decode(text.lstrip())
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        print(f"[warn] {PREFS_PATH.name} 解析失敗,改用空白偏好。")
        return {}


def save_prefs(patch: dict) -> dict:
    """把 patch 併入既有偏好後寫回(各機獨立檔)。回傳併後的完整偏好。全程在 _PREFS_LOCK 內
    序列化(擋並發 POST 的同檔競態),並用『寫暫存檔再 replace』的原子寫(避免讀到寫一半的檔,
    也吸收 Google Drive 虛擬磁碟對直接覆寫偶發的 [Errno 22]);replace 偶發失敗就短暫重試。"""
    with _PREFS_LOCK:
        prefs = load_prefs()
        prefs.update(patch)
        write_json_atomic(PREFS_PATH, prefs)
        return prefs


def machine_label(mid: str) -> str:
    """把機器代號換成 machines.json 裡的友善名稱(如「桌機」);沒對照就原樣顯示。"""
    m = read_json(MACHINES_PATH, default={})
    v = m.get(mid) if isinstance(m, dict) else None
    return str(v) if v else mid


# ── 設定檔 ────────────────────────────────────────────────────────
# summary_<日期>_<時間>[_<機器>].csv;舊檔沒有機器後綴也照樣比對得到。
SUMMARY_RE = re.compile(r"summary_(\d{8})_\d{6}(?:_(.+))?\.csv$")

DEFAULT_CONFIG = {
    "quota_gb": 2.0,
    "port": 8765,
    "sample_interval_sec": 60,
    # 內網排除/暴增歸因靠高頻 ESTATS 探測(與 60s NIC 計費解耦);內網很快、FTP 資料連線
    # 一檔一關,探測間隔愈短愈接得住短命連線,故設 1 秒,把漏算窗口從 60 秒縮到幾秒。
    "lan_probe_interval_sec": 1,
    "exclude_nics": ["Loopback", "isatap", "Teredo"],

    # ── 排除內網(私有 IP)流量 ──
    # 走內網(NAS/區網 FTP 等)的傳輸不該算進對外額度。v6.0 起改用 Windows ESTATS:高頻
    # (lan_probe_interval_sec)取每條 TCP 連線累積的收/發 byte,按對方 IP 判內/外網,落在
    # lan_ranges 的連線其增量從當日額度扣除(各方向再 clamp 到實際 NIC 增量)。逐連線計量,
    # 同支程式的內、外網連線會各自分開計。需以管理員身分啟動(ESTATS 啟用收集是特權操作);
    # 非管理員時此功能停用、總流量照常計。
    # ★ 本環境拓樸:對外走 10.65.x.x(含 GPSS 檢索主機 10.65.1.120~123,要計入),
    # 內網有 10.75.x.x 等多個 10.x 網段。lan_ranges 與 count_ranges 必須一起看:
    # 6.0.0 時 lan_ranges 只排除 10.75/16,結果其他 10.x 內網(FileZilla/SFTP)漏排除、
    # 單日額度一度衝到 ~6GB/303%。7.0.2 改成用 10.0.0.0/8 涵蓋所有 10.x,
    # 再靠 count_ranges(強制計入白名單,優先於 lan_ranges)把 10.65 釘死成永不被排除。
    # 所以這裡寫 10.0.0.0/8 是刻意的,不是漏改 —— 縮回 10.75/16 會讓那個 bug 復發。
    "exclude_lan": True,             # 預設開(此為預設;實際以本機 prefs 為準,可在頁面切換)
    "lan_ranges": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"],
    "count_ranges": ["10.65.0.0/16"],

    "max_count_per_run": 2000,
    "gpss_dir": "program/gpss",
    "gpss_script": "gpss_api_query.py",          # API 檢索(預設)
    "gpss_script_web": "gpss_web_query.py",      # 傳統(前端)檢索
    "gpss_python": "py -3.13",

    # ── API 額度顯示(官方前台公告值)──
    # 時段輸出總筆數:於 08:00/18:00(時段邊界)重置;單筆案號每小時:每整點重置。
    "dl_cap_busy": 10000,        # 上班時段「時段輸出總筆數」上限
    "dl_cap_offpeak": 30000,     # 下班時段「時段輸出總筆數」上限
    "case_cap_busy": 300,        # 上班「單筆案號每小時輸出筆數」上限
    "case_cap_offpeak": 1000,    # 下班「單筆案號每小時輸出筆數」上限
    "busy_start_hour": 8,
    "busy_end_hour": 18,

    # ── 限速偵測(網速)──
    # 超過每日額度後局網會「降速」(連得到但很慢);用網速來判斷現在有沒有被限速、
    # 申請的加給有沒有生效。被動:看最近 N 分鐘觀測到的最高下載速率(只在有流量時準)。
    # 主動:按「測速」從測速點抓一小段量實際達到的速率(較可靠,會耗一點流量)。
    "speed_floor_mbps": 5.0,         # 低於此(Mbps)判定「疑似被限速」(依妳平常正常網速調)
    "peak_window_min": 10,           # 被動「最高下載速率」的觀測視窗(分鐘)
    "speedtest_url": "https://speed.cloudflare.com/__down",  # 測速來源(若被擋可換)
    "speedtest_bytes": 8388608,      # 主動測速最多抓多少位元組(8MB;快線秒下、慢線會被 timeout 截斷)
    "speedtest_timeout": 6,          # 主動測速最長秒數(被限速時用這個時間讀到低速天花板就停)

    # ── 流量暴增預警 ──
    # 某次取樣區間的用量(收+發)若同時 ①超過絕對門檻 surge_min_mb,且 ②達近期中位數的
    # surge_factor 倍,就判定為「暴增」並試著歸因到某支程式(看哪些「有 inet 連線的程序」
    # 在這段期間 io_counters 增量最多 —— Windows 上 psutil 無每程序網路量,這是最佳推估)。
    # 中位數基準會隨持續下載自然升高、加上 cooldown,避免同一波暴增反覆洗版。
    "surge_enabled": True,           # 預設開(此為預設;實際以本機 prefs 為準,可在頁面切換)
    "surge_min_mb": 50,              # 單一取樣區間用量達此值(MB)才算暴增候選(避開閒置小波動)
    "surge_factor": 4.0,             # 且需達近期中位數的幾倍
    "surge_window_min": 30,          # 計算基準中位數的回看視窗(分鐘)
    "surge_cooldown_sec": 300,       # 同一波暴增最短重報間隔(秒),避免持續下載每分鐘洗版
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"[warn] config.json 讀取失敗,改用預設值: {e}")
    else:
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return cfg


def gpss_path(cfg: dict, name: str) -> Path:
    return (BASE / cfg["gpss_dir"]) / name


def _gpss_module():
    """延遲載入 gpss_api_query(program/gpss 套件),
    重用它的驗證碼池函式(load_keys / resolve_current_key / write_active_key /
    _mask_code);載入失敗回 None。"""
    try:
        from gpss import gpss_api_query
        return gpss_api_query
    except Exception:
        return None


# ============================================================
# 爬取統計 / 進度計算(資料來源:output 的 summary CSV + run_log.jsonl)
# ============================================================
def scan_output_daily(cfg: dict) -> dict:
    """彙整每日總筆數/流量,並拆出各機明細。

    資料來源:各機 output/summary_<機器>_<月>.xlsx(一天一張工作表)+ 尚未併入的
    summary_*.csv —— 都交給 output_store.daily_counts 處理(依表頭對齊欄位,任何
    舊格式都算得對;Excel 與殘留 CSV 不重複)。筆數只計「成功檢索(OK / 查無)」;
    失敗列不算筆數,但耗用位元組仍計入流量。

    回傳 {date: {"queries":int, "bytes":int,
                 "machines": {label: {"queries":int, "bytes":int}}}}。
    """
    out_dir = BASE / cfg["gpss_dir"] / "output"
    daily = {}
    if not out_dir.exists():
        return daily
    if output_store is None:
        return _scan_output_csv_only(out_dir)   # 沒有 output_store 就退回純 CSV 掃描
    for (date, mid), v in output_store.daily_counts(out_dir).items():
        label = machine_label(mid) if mid else "未標記"
        acc = daily.setdefault(date, {"queries": 0, "bytes": 0, "machines": {}})
        acc["queries"] += v["queries"]
        acc["bytes"] += v["bytes"]
        macc = acc["machines"].setdefault(label, {"queries": 0, "bytes": 0})
        macc["queries"] += v["queries"]
        macc["bytes"] += v["bytes"]
    return daily


def _scan_output_csv_only(out_dir) -> dict:
    """退路:openpyxl/output_store 不可用時,只掃 summary_*.csv(舊行為)。"""
    daily = {}
    for p in out_dir.glob("summary_*.csv"):
        m = SUMMARY_RE.search(p.name)
        if not m:
            continue
        if m.group(2) and not MID_RE.match(m.group(2)):
            continue                     # 衝突複本 CSV(機器代號帶空白/括號),跳過
        d = m.group(1)
        date = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        label = machine_label(m.group(2)) if m.group(2) else "未標記"
        q, b = 0, 0
        try:
            with p.open("r", encoding="utf-8-sig", newline="") as f:
                rdr = csv.reader(f)
                next(rdr, None)
                for row in rdr:
                    if len(row) < 5:
                        continue
                    if row[2].startswith("OK") or row[2] == "NO_RESULT":
                        q += 1
                    try:
                        b += int(row[4])
                    except (ValueError, IndexError):
                        pass
        except Exception:
            continue
        acc = daily.setdefault(date, {"queries": 0, "bytes": 0, "machines": {}})
        acc["queries"] += q
        acc["bytes"] += b
        macc = acc["machines"].setdefault(label, {"queries": 0, "bytes": 0})
        macc["queries"] += q
        macc["bytes"] += b
    return daily


RUNLOG_FILE_RE = re.compile(r"run_log_(.+)\.jsonl$")
# 機器代號只會是 [A-Za-z0-9-];帶空白/括號的(如 run_log_<機器> (1).jsonl)是
# Google Drive 同步衝突複本,整份跳過,否則執行時間會被重複計入(假機器/灌水)。
MID_RE = re.compile(r"^[A-Za-z0-9-]+$")


# run_log 解析的行內記憶體快取(依檔案 mtime+size 命中):run_log 只會在尾端追加、不重寫,
# 但每次統計請求原本都把整份重讀並逐行 json.loads,且檔案隨月份無限長大。改為依簽章快取
# 「每日每機原始秒數」:簽章沒變就直接重用,簽章變了才重解析整份(讀取仍每次重算機器標籤,
# 因為 machines.json 的友善名稱對應可能在程序執行中被改)。程序常駐,跨請求重用。
_RUNLOG_CACHE = {}


def _run_elapsed_for_file(p, file_mid) -> dict:
    """解析單一 run_log 檔 → {date: {raw_mid: 秒數}}(raw_mid 為紀錄/檔名推出的機器代號,
    空字串代表未標記)。依簽章快取;讀不到檔時沿用舊快取(若有)。"""
    sig = _file_sig(p)
    ent = _RUNLOG_CACHE.get(p.name)
    if ent and ent.get("sig") == sig:
        return ent["by_date"]
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception:
        return ent["by_date"] if ent else {}
    by_date = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        date = rec.get("date")
        if not date:
            continue
        secs = float(rec.get("elapsed_sec", 0) or 0)
        mid = rec.get("machine") or file_mid or ""
        per = by_date.setdefault(date, {})
        per[mid] = per.get(mid, 0.0) + secs
    _RUNLOG_CACHE[p.name] = {"sig": sig, "by_date": by_date}
    return by_date


def load_run_elapsed() -> dict:
    """掃 data/run_log*.jsonl(各機一份,舊的單一 run_log.jsonl 也涵蓋),
    彙整每日執行秒數:總計 + 各機。機器標籤優先取紀錄的 machine 欄,
    其次從檔名 run_log_<機器>.jsonl 推出,都沒有則歸『未標記』。

    回傳 {date: {"total": float, "machines": {label: float}}}。
    """
    daily = {}
    for p in MACHINE_DATA_DIR.glob("run_log*.jsonl"):
        m = RUNLOG_FILE_RE.search(p.name)
        file_mid = m.group(1) if m else None
        if file_mid and not MID_RE.match(file_mid):
            continue                     # 衝突複本 run_log,跳過(避免執行時間重複計入)
        for date, per_mid in _run_elapsed_for_file(p, file_mid).items():
            d = daily.setdefault(date, {"total": 0.0, "machines": {}})
            for mid, secs in per_mid.items():
                label = machine_label(mid) if mid else "未標記"
                d["total"] += secs
                d["machines"][label] = d["machines"].get(label, 0.0) + secs
    return daily
