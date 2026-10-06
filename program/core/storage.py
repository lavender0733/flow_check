"""
資料層:設定 / 偏好 / 機器標籤
==============================
從 traffic_monitor.py 抽出。相依:paths(路徑常數)。
不呼叫 traffic_monitor 內的任何東西,維持單向相依(traffic_monitor → storage),避免循環匯入。
"""

import json
import threading

from common.jsonio import read_json, write_json_atomic
from core.paths import CONFIG_PATH, MACHINES_PATH, PREFS_PATH


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
    # 非管理員時此功能停用、總流量照常計。一律開啟,沒有開關(8.1.0 起)。
    # ★ 內網有 10.75.x.x 等多個 10.x 網段。6.0.0 時 lan_ranges 只排除 10.75/16,結果其他
    # 10.x 內網(FileZilla/SFTP)漏排除、單日額度一度衝到 ~6GB/303%。7.0.2 起用 10.0.0.0/8
    # 涵蓋所有 10.x —— 這裡寫 10.0.0.0/8 是刻意的,不是漏改,縮回 10.75/16 會讓那個 bug 復發。
    # count_ranges 是強制計入白名單(優先於 lan_ranges):落在 lan_ranges 內、但實際走對外
    # 線路要算進額度的網段寫在這裡。預設空清單,需要時在 config.json 加。
    "lan_ranges": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"],
    "count_ranges": [],
    # 本機(loopback):程式連同一台電腦上的另一支程式(如瀏覽器連本工具),不經網卡、不吃額度。
    # 落在這裡的連線既不算內網也不算外網,整筆不列入計算與歸因。
    "local_ranges": ["127.0.0.0/8", "::1/128"],

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

