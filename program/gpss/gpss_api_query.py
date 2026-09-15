"""
GPSS API Batch Query (批次版)
=============================

對官方 TIPO GPSS API(HTTPS GET)做批次檢索。和舊版最大的差異:

  舊版:一筆專利 = 一個請求(1000 筆 = 1000 個請求)。
  新版:一個請求一次查多筆(`PN=A OR B OR C …`,預設 30 筆/請求),
        1000 筆只需約 30 個請求。

為什麼這樣可以大幅降低「檢索失敗率」:
  1. TIPO 的四台節點(gpss1~4)會整台、持續性地壞掉一段時間(回 HTTP 500
     或「no ini」節點錯誤頁)。請求數降 30 倍 → 踩到壞節點的機會同步降 30 倍,
     而且整批換台重試只花一個請求。
  2. 壞掉的節點在「本輪執行內」持久跳到 dead 清單,不會像舊版只冷卻 90 秒後又
     繞回去再撞;四台同時壞時改成「等一下再重試整批」,而不是把清單燒成失敗。
  3. 速度依時段自動控速,落在官方每小時額度內(上班 300/時、非上班 1000/時),
     避免超額被擋(舊版寫死 4 秒 = 900/時,上班一定超額)。
  4. API 回 status=fail 時,連同 TIPO 的訊息(gpss-msg)一起記下來並另存一份,
     不再只記成看不出原因的「API_ERR: fail」。

讀取:
  - api_config.json            : 共用設定(patDB、batch_size、控速…)
  - api_keys.json              : 驗證碼池(多組 API 驗證碼;新增驗證碼就改這裡的 keys)
  - api_config_<機器>.json     : 本機設定(active_key = 目前選用哪一組;另同步該組 userCode)
  - patents.txt                : 檢索清單, 一行一個專利號(# = 註解)

切換驗證碼:不帶參數執行(python gpss_api_query.py)會列出驗證碼池讓妳選,
Enter 沿用上次、輸入 id 切換、按 N 新增一組。帶參數(網頁面板那樣)則沿用上次選的,
不打斷自動化。各組即時剩餘額度看網頁面板(8765);log 會記下用了哪一組(key_label)。

用法(沿用舊參數,網頁控制台不必改):
    python gpss_api_query.py            # patents.txt 全部
    python gpss_api_query.py 2000       # 查 2000 筆(清單較短就循環)
    python gpss_api_query.py 2000 2000  # 第 2001-4000 筆(count offset)

輸出:output/summary_<時刻>_<機器>.csv(idx,patent,status,hits,bytes,code),每批即時
flush。整批的位元組記在該批第一列、其餘列記 0,所以總流量精確、下游統計不變。
code 欄記本輪用的 userCode,讓額度統計依「執行當下的碼」歸戶(切換驗證碼也分帳正確)。
把 batch_size 設成 1 即等同舊版「一筆一請求」。

額度(官方文件):上班 08:00-18:00 每小時 300、上限 3000;非上班 每小時 1000、
上限 10000。新版預設把這視為「每小時可查的專利筆數」(quota_counts="pn",保守)。
若日後確認 TIPO 是以『請求數』計額度,把 quota_counts 改成 "request" 即可大幅加速。
"""

import os
import re
import sys
import csv
import json
import time
import random
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

# 別把 .pyc 寫進這個資料夾 —— 它在 Google Drive 同步夾裡,兩台機器(且 Python 版本不同)
# 各自產生的 __pycache__/*.pyc 會互相同步、產生衝突複本,白白佔用同步頻寬、拖慢真資料同步。
sys.dont_write_bytecode = True

# 把 program/ 放上 sys.path，讓 core / gpss / common / yearly_stats 都能以套件匯入。
# 這是全專案唯一動 sys.path 的地方（每個進入點各一行），取代原本散在模組內的路徑魔法。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.ident import machine_id, mask_code as _mask_code   # noqa: E402  (sys.path 設定後才能匯入)
from common.jsonio import read_json   # noqa: E402

try:
    from gpss import output_store   # 把零散的 summary CSV 併成各機一個 Excel(一天一張工作表)
except Exception:
    output_store = None


def _skip_consolidate() -> bool:
    """由網頁伺服器啟動時(GPSS_SKIP_CONSOLIDATE=1)略過自己併檔 —— 那個 Excel 在
    Google Drive 同步夾裡,load+save 整份很慢,會害「印完 summary 仍卡著不結束、網頁
    任務不自動停」。伺服器改在任務結束後用背景執行緒併檔。獨立用命令列跑(沒設這個
    環境變數)時照常自己併,行為不變。"""
    return os.environ.get("GPSS_SKIP_CONSOLIDATE") == "1"


MACHINE_ID = machine_id()

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "api_config.json"                       # 共用設定
MACHINE_CONFIG_PATH = BASE / f"api_config_{MACHINE_ID}.json"  # 本機設定(active_key + 目前選用的 userCode)
KEYS_PATH = BASE / "api_keys.json"                           # 驗證碼池(多組 API 驗證碼;新增驗證碼就改這裡)
PATENTS_PATH = BASE / "patents.txt"
OUTPUT_DIR = BASE / "output"
# data/machines/ 在專案根目錄;本檔在 program/gpss/ 下,往上兩層。
RUN_LOG_PATH = BASE.parents[1] / "data" / "machines" / f"run_log_{MACHINE_ID}.jsonl"

DEFAULT_CONFIG = {
    "userCode": "",
    "aps": [1, 2, 3, 4],
    "patDB": "TWA,TWB,USA,USB,JPA,JPB,JPD,EPA,EPB,KPA,KPB,CNA,CNB,CND,WO,OTA,OTB",
    "queryField": "PN",
    "expFld": "PN,AN,ID,AD,TI",
    "expFmt": "json",
    "timeout_seconds": 40,
    "retries": 2,

    # ── 批次 ──
    "batch_size": 30,             # 一個請求查幾筆 PN(設 1 = 等同舊版一筆一請求)

    # ── 控速(避開官方每小時額度)──
    "quota_counts": "pn",         # "pn":額度以專利筆數計(保守);"request":以請求數計
    "quota_per_hour_busy": 300,   # 上班時段每小時額度
    "quota_per_hour_offpeak": 1000,  # 非上班時段每小時額度
    "quota_safety": 0.9,          # 只用額度的這個比例,留安全邊際
    "busy_start_hour": 8,         # 上班時段起(含)
    "busy_end_hour": 18,          # 上班時段迄(不含)
    "min_request_delay": 2,       # 兩個請求間的最小間隔秒數

    # ── 壞節點 / 失敗處理 ──
    "node_recovery_wait": 60,     # 四台同時壞時,等多久再重試整批(秒)
    "max_node_waits": 10,         # 同一批最多等幾輪(超過就把這批記失敗)
    "stop_on_quota": True,        # 收到 Over download quantity/fail → 立即停止本輪(不暫停重試);稍後自行重跑
    "fail_pause_seconds": 300,    # (stop_on_quota=False 時)收到 fail 暫停多久再重試(秒)
    "max_quota_waits": 2,         # (stop_on_quota=False 時)同一批因 fail 最多暫停重試幾次
    "give_up_after_failed_batches": 3,  # 連續幾批整批失敗就停止本輪(避免空燒清單)

    "probe_fastest_ap": True,     # 開跑前探測各 AP,選最快(且正常)的起跑
}


def load_config() -> dict:
    """先讀共用 api_config.json,再用本機 api_config_<機器>.json 覆蓋(各機 userCode)。"""
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"[warn] api_config.json 讀取失敗,用預設值: {e}")
    else:
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    machine_cfg = {}
    if MACHINE_CONFIG_PATH.exists():
        try:
            machine_cfg = json.loads(MACHINE_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[warn] {MACHINE_CONFIG_PATH.name} 讀取失敗,改用共用設定: {e}")
    else:
        machine_cfg = {"userCode": cfg.get("userCode", "")}
        MACHINE_CONFIG_PATH.write_text(
            json.dumps(machine_cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"※ 已建立本機 API 設定檔 {MACHINE_CONFIG_PATH.name}"
              f"(沿用共用 userCode);如要這台用不同 API,改它即可。")

    for k, v in machine_cfg.items():
        if k == "userCode" and not str(v).strip():
            continue
        cfg[k] = v
    return cfg


# ─────────────────────────── 驗證碼池(可切換 / 可新增)───────────────────────────
#
# 設計重點:
#   - api_keys.json 是「驗證碼池」,集中放多組 API 驗證碼;要新增驗證碼,
#     只要在這個檔的 keys 陣列加一筆(或開跑選單按 N),不必再去動各機設定檔。
#   - 各機 api_config_<機器>.json 只記 active_key(目前選用哪一組的 id);同時
#     同步寫入該組的 userCode,讓 traffic_monitor 的「每組額度統計」沿用既有邏輯。
#   - 顯示與 log 一律遮罩(label + 前4碼),平常看不到全碼;切換靠 label/id,
#     永遠不必看明碼。

def save_keys(keys: list) -> None:
    KEYS_PATH.write_text(
        json.dumps({"keys": keys}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _build_keys_from_legacy() -> list:
    """第一次沒有 api_keys.json 時,從現有 api_config*.json 收集 userCode 自動建池
    (不弄丟既有設定)。共用設定的碼標 '共用',各機的碼以機器名當 label。"""
    seen = {}        # userCode -> label
    order = []

    def add(uc, label):
        uc = str(uc or "").strip()
        if not uc or uc in seen:
            return
        seen[uc] = label
        order.append(uc)

    shared = read_json(CONFIG_PATH, default={})
    if isinstance(shared, dict):
        add(shared.get("userCode", ""), "共用")
    for p in sorted(BASE.glob("api_config_*.json")):
        m = re.match(r"api_config_(.+)\.json$", p.name)
        if not m:
            continue
        cfg = read_json(p, default={})
        if isinstance(cfg, dict):
            add(cfg.get("userCode", ""), m.group(1))

    keys = []
    for i, uc in enumerate(order):
        kid = chr(ord("A") + i) if i < 26 else f"K{i}"
        keys.append({"id": kid, "label": seen[uc], "userCode": uc})
    return keys


def load_keys() -> list:
    """讀驗證碼池;不存在則從現有設定自動建立並寫檔。"""
    if KEYS_PATH.exists():
        try:
            keys = json.loads(KEYS_PATH.read_text(encoding="utf-8")).get("keys", [])
            keys = [k for k in keys if str(k.get("userCode", "")).strip()]
            if keys:
                return keys
        except Exception as e:
            print(f"[warn] api_keys.json 讀取失敗: {e}")
    keys = _build_keys_from_legacy()
    if keys:
        save_keys(keys)
        print(f"※ 已建立驗證碼池 {KEYS_PATH.name}(收集 {len(keys)} 組現有驗證碼)。"
              f"日後新增驗證碼:編輯這個檔,或在開跑選單按 N 新增。")
    return keys


def read_active_key_id():
    cfg = read_json(MACHINE_CONFIG_PATH, default={})
    return cfg.get("active_key") if isinstance(cfg, dict) else None


def write_active_key(key: dict) -> None:
    """把選定的 key 寫回本機設定:存 active_key(id),並同步 userCode
    (讓 traffic_monitor 的每組額度統計沿用既有的『機器→userCode』對應)。"""
    data = read_json(MACHINE_CONFIG_PATH, default={})
    if not isinstance(data, dict):
        data = {}
    data["active_key"] = key.get("id", "")
    data["userCode"] = key.get("userCode", "")
    MACHINE_CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def resolve_current_key(keys: list):
    """決定本機目前該用哪一組:先看 active_key,再以既有 userCode 比對(遷移用),
    都沒有就用第一組。"""
    if not keys:
        return None
    active_id = read_active_key_id()
    if active_id:
        for k in keys:
            if k.get("id") == active_id:
                return k
    cfg = read_json(MACHINE_CONFIG_PATH, default={})
    legacy_uc = str(cfg.get("userCode", "")).strip() if isinstance(cfg, dict) else ""
    if legacy_uc:
        for k in keys:
            if k.get("userCode") == legacy_uc:
                return k
    return keys[0]


def _add_key_interactive(keys: list):
    """選單按 N:輸入名稱 + 驗證碼,存進 api_keys.json,回傳新 key。"""
    try:
        label = input("  新驗證碼名稱(label,例如『備用-3』): ").strip()
        uc = input("  驗證碼(userCode): ").strip()
    except EOFError:
        return None
    if not uc:
        print("  未輸入驗證碼,取消新增。")
        return None
    used = {str(k.get("id", "")).upper() for k in keys}
    nid = next((chr(c) for c in range(ord("A"), ord("Z") + 1) if chr(c) not in used),
               f"K{len(keys)}")
    nk = {"id": nid, "label": label or nid, "userCode": uc}
    keys.append(nk)
    save_keys(keys)
    print(f"  已新增 [{nid}] {nk['label']}  {_mask_code(uc)} → 寫入 {KEYS_PATH.name}")
    return nk


def _prompt_key_choice(keys: list, current):
    """列出所有驗證碼(label + 遮罩碼)讓使用者選;Enter=沿用、id=切換、N=新增。
    即時剩餘額度請看網頁面板,CLI 不耦合 traffic_monitor。"""
    cur_id = current.get("id") if current else None
    print()
    print("可用 API 驗證碼(各組即時剩餘額度看網頁面板 http://127.0.0.1:8765):")
    for k in keys:
        mark = "►" if k.get("id") == cur_id else " "
        print(f"  {mark} [{k.get('id')}] {k.get('label', '')}  {_mask_code(k.get('userCode', ''))}")
    cur_label = f"[{cur_id}] {current.get('label', '')}" if current else "(無)"
    try:
        raw = input(f"選哪一組?(Enter=沿用 {cur_label};輸入 id 切換;N=新增): ").strip()
    except EOFError:
        return current
    if not raw:
        return current
    if raw.upper() == "N":
        return _add_key_interactive(keys) or current
    for k in keys:
        if str(k.get("id", "")).upper() == raw.upper():
            return k
    print(f"  找不到 '{raw}',沿用 {cur_label}。")
    return current


def select_active_key(cfg: dict) -> dict:
    """載入驗證碼池、決定本機目前用哪組;互動模式(沒帶命令列參數)會出選單。
    把選定的碼塞回 cfg['userCode'],並記下 id/label 供顯示與 log 使用。"""
    keys = load_keys()
    if not keys:
        return cfg
    current = resolve_current_key(keys)
    interactive = (len(sys.argv) <= 1) and bool(getattr(sys.stdin, "isatty", lambda: False)())
    chosen = _prompt_key_choice(keys, current) if interactive else current
    if chosen:
        write_active_key(chosen)
        cfg["userCode"] = chosen.get("userCode", "")
        cfg["_key_id"] = chosen.get("id", "")
        cfg["_key_label"] = chosen.get("label", "")
    return cfg


def load_patents() -> list:
    if not PATENTS_PATH.exists():
        return []
    out = []
    for line in PATENTS_PATH.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def fmt_dur(seconds: float) -> str:
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _int_arg(value: str, fallback: int) -> int:
    try:
        return max(0, int(value))
    except ValueError:
        print(f"參數 '{value}' 不是數字,改用 {fallback}")
        return fallback


def resolve_run_params(default: int) -> tuple:
    if len(sys.argv) > 1:
        count = _int_arg(sys.argv[1], default) or default
        offset = _int_arg(sys.argv[2], 0) if len(sys.argv) > 2 else 0
        return count, offset
    try:
        raw = input(f"要查詢幾筆?(直接 Enter = {default}): ").strip()
    except EOFError:
        return default, 0
    count = _int_arg(raw, default) if raw else default
    return (count or default), 0


# ─────────────────────────── 回應判讀 ───────────────────────────

def _norm_pn(s: str) -> str:
    """把專利號正規化成只剩英數大寫,方便比對(忽略國別前綴差異、種類碼後綴)。"""
    return re.sub(r"[^0-9A-Za-z]", "", s or "").upper()


def parse_records(data: dict) -> list:
    """從成功的 JSON 取出每筆紀錄的 (publication doc-number, application doc-number)。"""
    api = data.get("gpss-API") if isinstance(data, dict) else None
    if not isinstance(api, dict):
        return []
    pat = api.get("patent")
    if not isinstance(pat, dict):
        return []
    contents = pat.get("patentcontent")
    if isinstance(contents, dict):
        contents = [contents]
    if not isinstance(contents, list):
        return []
    recs = []
    for c in contents:
        if not isinstance(c, dict):
            continue
        pub = ((c.get("publication-reference") or {}).get("doc-number")) or ""
        app = ((c.get("application-reference") or {}).get("doc-number")) or ""
        recs.append((str(pub), str(app)))
    return recs


def interpret_batch(raw: bytes) -> tuple:
    """回傳 (status, records_or_None, bytes)。

    status:
      "OK"             → 正常,records 為該批所有紀錄(可能 0 筆)
      "NODE_ERR"       → 節點錯誤頁(no ini / gpss_api.c);換台可能有救
      "API_ERR: …"     → API 回 status!=success(含 TIPO 訊息);疑似超額/維護
      "AUTH_OR_ERROR"  → 驗證/授權類錯誤(換台沒用,應停)
      "NON_JSON"       → 非 JSON 且無法歸類
    """
    n = len(raw)
    text = raw.decode("utf-8", "replace")
    low = text.lower()
    try:
        data = json.loads(text)
    except Exception:
        if any(w in low for w in ("gpss_api.ini", "gpss_api.c", "no ini")):
            return ("NODE_ERR", None, n)
        if any(w in text for w in ("錯誤", "驗證", "無效", "未授權")) or "error" in low:
            return ("AUTH_OR_ERROR", None, n)
        return ("NON_JSON", None, n)

    api = data.get("gpss-API") if isinstance(data, dict) else None
    if isinstance(api, dict):
        st = str(api.get("status", "")).strip()
        if st and st.lower() != "success":
            # 把 TIPO 給的訊息一併帶出(整個 api 區塊很小,排除 patent 後串起來)。
            extra = "; ".join(
                f"{k}={v}" for k, v in api.items()
                if k != "patent" and not isinstance(v, (dict, list))
            )
            return (f"API_ERR: {extra or st}", None, n)
        return ("OK", parse_records(data), n)

    return ("NON_JSON", None, n)


# ─────────────────────────── 請求 ───────────────────────────

def query_batch_raw(cfg: dict, numbers: list, ap: int, field: str = "PN") -> bytes:
    base = f"https://tiponet.tipo.gov.tw/gpss{ap}/gpsskmc/gpss_api"
    q = " OR ".join(numbers)
    expqty = min(1000, max(50, len(numbers) * 10))   # 留每筆多筆同族的空間
    params = {
        "userCode": cfg["userCode"],
        "patDB": cfg["patDB"],
        field: q,                          # 查詢欄位:PN(公開公告號)或 AN(申請號)
        "expFld": cfg["expFld"],
        "expFmt": cfg["expFmt"],
        "expQty": expqty,
    }
    # 用 quote(空白→%20),不要用預設的 quote_plus(空白→'+');TIPO 不會把 '+'
    # 還原成空白,會讓 "A OR B" 變成查無結果。
    url = base + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    req = urllib.request.Request(url, headers={"User-Agent": "flow-check-gpss/2.0"})
    with urllib.request.urlopen(req, timeout=cfg["timeout_seconds"]) as resp:
        return resp.read()


def attempt_batch(cfg: dict, numbers: list, ap: int, field: str = "PN") -> tuple:
    """對單一節點送一批(同節點的純連線/逾時錯誤會重試 retries 次)。
    回傳 (status, records_or_None, bytes, raw_or_None)。"""
    retries = max(1, int(cfg.get("retries", 2)))
    status, raw = "EXCEPTION", None
    for attempt in range(1, retries + 1):
        try:
            raw = query_batch_raw(cfg, numbers, ap, field)
            status, recs, nbytes = interpret_batch(raw)
            return status, recs, nbytes, raw
        except urllib.error.HTTPError as e:
            status, raw = f"HTTP_{e.code}", None
        except urllib.error.URLError as e:
            status, raw = f"NET_ERROR: {e.reason}", None
        except Exception as e:
            status, raw = f"EXCEPTION: {e}", None
        if attempt < retries:
            time.sleep(2 * attempt)
    return status, None, 0, None


def is_node_failure(status: str) -> bool:
    """這個失敗是『節點層級、換台可能有救』嗎?(節點錯誤頁/連線錯誤/5xx/429/非 JSON)"""
    if status in ("NODE_ERR", "NON_JSON"):
        return True
    if status.startswith(("NET_ERROR", "EXCEPTION")):
        return True
    if status.startswith("HTTP_"):
        try:
            code = int(status[5:].split(":")[0].strip())
        except ValueError:
            return True
        return code == 429 or code >= 500
    return False


def probe_fastest_ap(cfg: dict, aps: list, probe_numbers: list) -> tuple:
    """開跑前對各候選 AP 送一批真實查詢、量回應時間,挑回應最快(且正常)的起跑。
    回傳 (最快的 AP 或 None, 探測耗用位元組)。"""
    attempts = max(1, min(3, int(cfg.get("retries", 2))))
    best_lat = {}
    probe_bytes = 0
    for a in aps:
        for i in range(attempts):
            try:
                t0 = time.monotonic()
                raw = query_batch_raw(cfg, probe_numbers, a)
                lat = time.monotonic() - t0
                status, _, nbytes = interpret_batch(raw)
                probe_bytes += nbytes
                if status == "OK":
                    best_lat[a] = lat
                    print(f"  探測 gpss{a}:{lat:.2f}s（OK）", flush=True)
                    break
                print(f"  探測 gpss{a}:第 {i + 1} 次 {status}", flush=True)
            except Exception as e:
                print(f"  探測 gpss{a}:第 {i + 1} 次 失敗（{e}）", flush=True)
            if i < attempts - 1:
                time.sleep(1.0)
        time.sleep(0.5)
    if best_lat:
        return min(best_lat, key=best_lat.get), probe_bytes
    return None, probe_bytes


# ─────────────────────────── 控速 ───────────────────────────

def is_busy_hours(cfg: dict) -> bool:
    hr = datetime.now().hour
    return int(cfg["busy_start_hour"]) <= hr < int(cfg["busy_end_hour"])


def request_delay(cfg: dict, batch_len: int) -> float:
    """依時段與額度算出兩請求之間應間隔幾秒,確保不超過官方每小時額度。"""
    busy = is_busy_hours(cfg)
    budget = float(cfg["quota_per_hour_busy"] if busy else cfg["quota_per_hour_offpeak"])
    budget *= float(cfg.get("quota_safety", 0.9))
    if budget <= 0:
        return float(cfg.get("min_request_delay", 2))
    if str(cfg.get("quota_counts", "pn")).lower() == "request":
        delay = 3600.0 / budget                      # 額度以請求數計
    else:
        delay = batch_len / (budget / 3600.0)        # 額度以專利筆數計
    return max(delay, float(cfg.get("min_request_delay", 2)))


# ─────────────────────────── 主流程 ───────────────────────────

def append_run_log(rec: dict):
    """附加一行到本機 run_log_<機器>.jsonl(事件或執行摘要都用這個)。
    事件行帶 ts/date/machine/event,即時寫入,即使本輪稍後被硬殺也留得住,
    方便事後排查(例如哪台、哪支碼、何時撞 Over download quantity、何時恢復)。"""
    try:
        RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with RUN_LOG_PATH.open("a", encoding="utf-8") as lf:
            lf.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[warn] 無法寫入執行紀錄: {e}")


def _masked_user_code(cfg: dict) -> str:
    return _mask_code(cfg.get("userCode", ""))


def log_quota_event(cfg: dict, field: str, status: str, ap: int, output_so_far: int):
    """撞到超額(Over download quantity / API_ERR)時即時記一筆事件。"""
    now = datetime.now()
    append_run_log({
        "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "machine": MACHINE_ID,
        "event": "over_quota",
        "userCode": _masked_user_code(cfg),
        "key_label": cfg.get("_key_label", ""),  # 哪一組驗證碼撞牆,事後對帳用
        "field": field,                   # PN / AN
        "ap": ap,
        "message": status,                # 含 TIPO 原訊息(gpss-msg)
        "output_this_run": output_so_far,  # 本輪到撞牆前已輸出幾筆(OK)
    })


def quota_recovery_pending() -> tuple:
    """掃本機 run_log:最近一次 over_quota 之後若還沒記 recovered,回 (True, 那次ts);
    否則 (False, None)。用來在『撞牆後第一次又成功』時補記 recovered,量出恢復窗口。"""
    last_over = last_rec = None
    try:
        for line in RUN_LOG_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            ev = rec.get("event")
            if ev == "over_quota":
                last_over = rec.get("ts") or last_over
            elif ev == "recovered":
                last_rec = rec.get("ts") or last_rec
    except Exception:
        return False, None
    if last_over and (not last_rec or last_over > last_rec):
        return True, last_over
    return False, None


def log_recovered_event(cfg: dict, ap: int, over_ts: str):
    """撞牆後第一次又成功時記一筆;after_over_quota 與 ts 相減即恢復窗口。"""
    now = datetime.now()
    append_run_log({
        "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "machine": MACHINE_ID,
        "event": "recovered",
        "userCode": _masked_user_code(cfg),
        "key_label": cfg.get("_key_label", ""),
        "ap": ap,
        "after_over_quota": over_ts,      # 上次撞牆時刻
    })


def resolve_batch(cfg: dict, numbers: list, ap: int, dead: set, aps: list,
                  field: str = "PN") -> tuple:
    """用指定欄位(PN/AN)對一批求結果:在節點間換台、必要時等待重試,直到 OK 或放棄。
    就地更新 dead(本輪壞節點)。回傳 (status, records_or_None, consumed_bytes, ap, raw)。
      status: OK / ALL_NODES_DOWN / "API_ERR: …" / AUTH_OR_ERROR / NON_JSON …
      raw   : OK 時=成功回應、API_ERR 時=該錯誤回應(供存樣本/錯誤檔),其餘 None。"""
    consumed = 0
    tried = set()
    node_waits = quota_waits = 0
    while True:
        if ap in dead or ap in tried:
            cand = [a for a in aps if a not in dead and a not in tried]
            if not cand:
                if len(dead) >= len(aps) and node_waits < int(cfg["max_node_waits"]):
                    node_waits += 1
                    print(f"##WARN## 四台 AP 暫時都異常,{int(cfg['node_recovery_wait'])}s 後"
                          f"重試本批(第 {node_waits} 次)。", flush=True)
                    time.sleep(float(cfg["node_recovery_wait"]))
                    dead.clear()
                    tried.clear()
                    ap = random.choice(aps)
                    continue
                return "ALL_NODES_DOWN", None, consumed, ap, None
            ap = cand[0]

        st, recs, nbytes, raw = attempt_batch(cfg, numbers, ap, field)
        consumed += nbytes
        tried.add(ap)

        if st == "OK":
            if dead:                       # 換到正常節點 → 清警告橫幅
                print(f"##CLEAR## 已切換到正常節點 gpss{ap},恢復檢索。", flush=True)
            return "OK", recs, consumed, ap, raw

        if is_node_failure(st):
            dead.add(ap)
            print(f"##WARN## 節點 gpss{ap} 異常({st}),本輪改不再用它,換台重試本批。", flush=True)
            continue

        if st.startswith("API_ERR"):
            print(f"##WARN## TIPO 回覆:{st}", flush=True)
            if cfg.get("stop_on_quota", True):
                return st, None, consumed, ap, raw   # 不暫停,直接回,由呼叫端停整輪
            if quota_waits < int(cfg["max_quota_waits"]):
                quota_waits += 1
                pause = float(cfg["fail_pause_seconds"])
                print(f"##WARN## 疑似超額/維護,暫停 {pause:.0f}s 後重試本批"
                      f"(第 {quota_waits} 次)。", flush=True)
                time.sleep(pause)
                tried.clear()              # 暫停後各台都重新給機會
                continue
            return st, None, consumed, ap, raw

        # AUTH_OR_ERROR 等:換台沒用,直接回(由呼叫端停整輪)。
        return st, None, consumed, ap, raw


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def run(count: int, offset: int):
    cfg = load_config()
    cfg = select_active_key(cfg)   # 選/沿用驗證碼(互動時出選單),設定 cfg["userCode"]
    if not str(cfg.get("userCode", "")).strip():
        print("⛔ 驗證碼池是空的。請編輯 api_keys.json 在 keys 加一組 {id,label,userCode},")
        print("   或執行時在選單按 N 新增。(申請:TIPO GPSS API 線上申請,一個 email 一組)")
        return

    patents = load_patents()
    if not patents:
        print("⛔ patents.txt 沒有任何專利號。請在網頁控制台的『檢索清單』填入後儲存,再執行。")
        return

    run_list = [patents[i % len(patents)] for i in range(offset, offset + count)]
    total = len(run_list)
    end_idx = offset + total

    batch_size = max(1, int(cfg.get("batch_size", 30)))
    aps = list(dict.fromkeys(int(x) for x in cfg.get("aps", [1, 2, 3, 4]))) or [1]
    ap = random.choice(aps)
    dead = set()                       # 本輪已判定壞掉的節點(持久跳過)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if output_store is not None and not _skip_consolidate():
        try:
            output_store.consolidate(MACHINE_ID)
        except Exception:
            pass
    summary = OUTPUT_DIR / f"summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{MACHINE_ID}.csv"

    print(f"GPSS API 批次檢索  |  {total} 筆(第 {offset + 1}-{end_idx})  |  本機:{MACHINE_ID}")
    print(f"驗證碼:[{cfg.get('_key_id', '?')}] {cfg.get('_key_label', '')}  {_masked_user_code(cfg)}")
    print(f"DB: {cfg['patDB']}  |  欄位:{cfg['queryField']}  |  每請求 {batch_size} 筆")
    print(f"時段:{'上班(額度較緊)' if is_busy_hours(cfg) else '非上班'}  |  "
          f"控速:每請求約 {request_delay(cfg, batch_size):.0f}s（quota_counts={cfg.get('quota_counts')}）")

    # 開跑前探測:選回應最快(且正常)的節點起跑。
    probe_bytes = 0
    if cfg.get("probe_fastest_ap", True) and len(aps) > 1:
        print(f"開跑前探測 {len(aps)} 台 AP(送一小批真實查詢,選最快起跑)…", flush=True)
        best, probe_bytes = probe_fastest_ap(cfg, aps, run_list[:min(batch_size, 5)])
        if best is not None:
            ap = best
            print(f"→ 選用回應最快的 gpss{ap} 起跑。", flush=True)
        else:
            print(f"→ 四台暫時都拿不到正常回應,改隨機用 gpss{ap},執行中會自動換台。", flush=True)

    print(f"本次起始 AP:gpss{ap}(共 {len(aps)} 台候選;壞節點自動跳過 + 整批換台重試)")
    print(f"Output: {summary}")
    print("-" * 60)

    start = time.time()
    total_bytes = probe_bytes
    success = no_result = failed = 0
    sample_saved = False
    fail_dump_saved = False
    consec_failed_batches = 0
    stop_reason = None
    # 若上次撞牆後還沒記過「恢復」,這輪第一次成功時補記一筆(量恢復窗口)。
    pending_recovery, prior_over_ts = quota_recovery_pending()
    recovery_logged = False

    detect = os.environ.get("GPSS_LIMIT_DETECT", "1") == "1"

    # code 欄記下本輪實際使用的 userCode:額度統計改以「執行當下寫死的碼」歸戶,
    # 中途在面板切換驗證碼也能正確分帳(不再事後查機器目前選哪組而把舊輸出歸錯碼)。
    run_code = str(cfg.get("userCode", "")).strip()

    f = summary.open("w", encoding="utf-8-sig", newline="")
    w = csv.writer(f)
    w.writerow(["idx", "patent", "status", "hits", "bytes", "code"])
    f.flush()

    batches = list(chunked(run_list, batch_size))

    try:
        done = 0
        for bi, batch in enumerate(batches):
            base_pos = bi * batch_size            # 本批第一筆在 run_list 的位置

            # ── 第一段:用 PN(公開公告號)查整批(換台/重試由 resolve_batch 處理)──
            status, records, consumed, ap, raw = resolve_batch(cfg, batch, ap, dead, aps, "PN")
            if status == "OK" and not sample_saved and raw:
                try:
                    (OUTPUT_DIR / "_sample_response.json").write_bytes(raw)
                except Exception:
                    pass
                sample_saved = True
            if status == "OK" and pending_recovery and not recovery_logged:
                log_recovered_event(cfg, ap, prior_over_ts)
                recovery_logged = True
                print(f"##CLEAR## 額度已恢復(上次 {prior_over_ts} 撞牆),恢復檢索。", flush=True)
            if status.startswith("API_ERR") and not fail_dump_saved and raw:
                try:
                    (OUTPUT_DIR / "_fail_response.json").write_bytes(raw)
                except Exception:
                    pass
                fail_dump_saved = True
            if status.startswith("API_ERR"):
                log_quota_event(cfg, "PN", status, ap, success)
                if cfg.get("stop_on_quota", True):
                    stop_reason = (f"TIPO 回覆額度已滿/維護({status}),已停止本輪"
                                   f"(稍後額度恢復再重新執行即可)。")
            # AUTH_OR_ERROR 等換台沒用的錯誤 → 停整輪(ALL_NODES_DOWN 仍算失敗批、續跑)。
            if status not in ("OK", "ALL_NODES_DOWN") and not status.startswith("API_ERR"):
                stop_reason = f"收到無法靠換台解決的錯誤({status}),停止本輪。"

            ok_batch = (status == "OK")
            hits_of = {n: 0 for n in batch}
            via_an = set()                        # 靠申請號(AN)才命中的號(只影響 console 標示)
            if ok_batch:
                # 第一段以 publication(公開號)比對命中。
                pubs_norm = [_norm_pn(p) for (p, a) in (records or [])]
                for number in batch:
                    k = _norm_pn(number)
                    h = sum(1 for p in pubs_norm if k and k in p)
                    if h:
                        hits_of[number] = h

                # ── 第二段:第一段沒中的(多半是申請號),改用 AN(申請號)再查一次 ──
                # 查無不耗輸出額度,所以只對「沒中的」補一次 AN,額度幾乎不增。
                unmatched = [n for n in batch if hits_of[n] == 0]
                if unmatched:
                    an_st, an_recs, an_bytes, ap, an_raw = resolve_batch(
                        cfg, unmatched, ap, dead, aps, "AN")
                    consumed += an_bytes
                    if an_st.startswith("API_ERR") and not fail_dump_saved and an_raw:
                        try:
                            (OUTPUT_DIR / "_fail_response.json").write_bytes(an_raw)
                        except Exception:
                            pass
                        fail_dump_saved = True
                    if an_st.startswith("API_ERR"):
                        log_quota_event(cfg, "AN", an_st, ap, success)
                        if cfg.get("stop_on_quota", True):
                            stop_reason = (f"TIPO 回覆額度已滿/維護({an_st}),已停止本輪"
                                           f"(稍後額度恢復再重新執行即可)。")
                    if an_st == "OK" and an_recs:
                        # 第二段以 application(申請號)比對命中。
                        apps_norm = [_norm_pn(a) for (p, a) in an_recs]
                        for number in unmatched:
                            k = _norm_pn(number)
                            h = sum(1 for a in apps_norm if k and k in a)
                            if h:
                                hits_of[number] = h
                                via_an.add(number)

            # ── 把整批結果寫成每筆一列(整批位元組只記在第一列,總流量才精確)──
            batch_success = batch_no_result = batch_failed = 0
            for j, number in enumerate(batch):
                idx = offset + base_pos + j + 1
                row_bytes = consumed if j == 0 else 0
                if ok_batch:
                    hits = hits_of[number]
                    if hits > 0:
                        row_status = f"OK ({hits} hits)"
                        batch_success += 1
                    else:
                        row_status = "NO_RESULT"
                        batch_no_result += 1
                    w.writerow([idx, number, row_status, hits, row_bytes, run_code])
                    print(f"[{idx:>5}/{end_idx}] gpss{ap}  {number:<20} -> {row_status}"
                          + ("  (AN)" if number in via_an else ""))
                else:
                    w.writerow([idx, number, status, 0, row_bytes, run_code])
                    batch_failed += 1
                    print(f"[{idx:>5}/{end_idx}] gpss{ap}  {number:<20} -> {status}")
            f.flush()

            success += batch_success
            no_result += batch_no_result
            failed += batch_failed
            total_bytes += consumed
            done += len(batch)

            # 連續整批失敗 → 停止本輪,避免空燒清單(額度用盡或 TIPO 全面異常時)。
            # 只有「可用上限偵測」開啟(detect)時才擋;關閉時照常往下跑(僅前面已印的 ##WARN##)。
            if ok_batch:
                consec_failed_batches = 0
            else:
                consec_failed_batches += 1
                if detect and stop_reason is None and consec_failed_batches >= int(cfg["give_up_after_failed_batches"]):
                    stop_reason = (f"連續 {consec_failed_batches} 批整批失敗,"
                                   f"疑似額度用盡或 TIPO 全面異常,已停止(剩餘未檢索)。")

            if stop_reason:
                print(f"##WARN## {stop_reason}", flush=True)
                break

            # 控速:本批不是最後一批才需要間隔。控速永遠生效(避免超額),
            # 不受 GPSS_LIMIT_DETECT 開關影響;detect 只管前面的延遲/失敗警告。
            if done < total:
                time.sleep(request_delay(cfg, len(batch)))
    except KeyboardInterrupt:
        print("\n--- 收到中止訊號,停止(結果已保存)---")
    finally:
        f.close()

    elapsed = time.time() - start
    print("-" * 60)
    print(f"Summary: {summary}")
    print(f"成功: {success}  |  查無: {no_result}  |  失敗: {failed}")
    print(f"網路流量總計: {human_bytes(total_bytes)} ({total_bytes:,} bytes)")
    print(f"執行時間: {fmt_dur(elapsed)}")
    if dead:
        print(f"本輪判定異常的節點: {'、'.join(f'gpss{a}' for a in sorted(dead))}")

    processed = success + no_result + failed
    try:
        now = datetime.now()
        rec = {
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            "date": now.strftime("%Y-%m-%d"),
            "queries": processed,
            "success": success,
            "no_result": no_result,
            "failed": failed,
            "bytes": total_bytes,
            "elapsed_sec": round(elapsed, 1),
            "ap": ap,
            "machine": MACHINE_ID,
            "method": "api",
            "code": run_code,                # 本輪實際用的 userCode → 月用量依碼歸戶(切換也正確)
            "summary": summary.name,
        }
        RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with RUN_LOG_PATH.open("a", encoding="utf-8") as lf:
            lf.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[warn] 無法寫入執行紀錄 run_log.jsonl: {e}")

    if not sample_saved:
        print("(沒有任何成功回應,_sample_response.json 未產生)")
    if fail_dump_saved:
        print("提示:TIPO 的 fail 回應已存到 output/_fail_response.json,可看真正原因(超額或維護)。")

    if output_store is not None and not _skip_consolidate():
        try:
            n = output_store.consolidate(MACHINE_ID)
            if n:
                print(f"已把本次結果併入 output/summary_{MACHINE_ID}_<本月>.xlsx(當天工作表)。")
        except Exception:
            pass


def main():
    count, offset = resolve_run_params(default=len(load_patents()) or 1)
    run(count, offset)


if __name__ == "__main__":
    main()
