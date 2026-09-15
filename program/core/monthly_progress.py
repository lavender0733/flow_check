# -*- coding: utf-8 -*-
"""
monthly_progress.py — 月檢索進度計算（手動下載版）

讀 data/exports/ 下你手動從後台下載的匯出檔（假 .xls、真 HTML 表格），
把「檢索次數」欄加總，算出：
  本月累積筆數 = 簡易版檢索次數總和 + API檢索次數總和
並對照月目標，估出本月剩餘工作日每天平均需檢索多少。

零依賴（不需 pandas / openpyxl）。可雙擊對應 .bat 直接看報告，
也被 traffic_monitor.py 以 compute() 匯入，供網頁「檢索進度」分頁顯示。

使用方式：
  - 每隔幾天上後台，把「下載範圍 = 本月1號~今天」的『簡易版』與『API』各下載一份，
    放進 data/exports/ 資料夾（檔名隨意，含「簡易版」「API」即可；程式取最新一份）。
  - 跑這支程式即印出進度報告。

注意：本估算只計『簡易版 + API』（簡易版佔前台絕大多數），
未含完整版/行動版/英簡易/英完整，故為偏保守（略低）的估計。
"""

import os
import re
import sys
import glob
import json
import datetime
import calendar
import threading
import time
from pathlib import Path

# 把 program/ 放上 sys.path，讓 core / gpss / common / yearly_stats 都能以套件匯入。
# 這是全專案唯一動 sys.path 的地方（每個進入點各一行），取代原本散在模組內的路徑魔法。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.paths import (BASE, EXPORTS_DIR, HISTORY_PATH, HOLIDAYS_PATH, MACHINE_DATA_DIR,
                        TARGETS_PATH, MACHINE_ID)
from common.ident import mask_code
from common.jsonio import file_sig as _file_sig, read_json, write_json_atomic

# 本檔位於 program/core/ 下;資料(data/、config.json)仍在專案根目錄 flow_check/。
BASE = str(BASE)
DATA = os.path.join(BASE, "data")

# ── 設定 ────────────────────────────────────────────────
DEFAULT_MONTHLY_TARGET = 450000   # 月目標筆數（targets.json 有 monthly_target 則以它為準）
SIMPLE_GLOB = "*簡易版*.xls"       # 簡易版匯出檔名樣式
API_GLOB = "*API*.xls"            # API 匯出檔名樣式
# 達標情況分析的快照只從此功能啟用後開始累積;不從既有匯出檔回推歷史。
# 依機器分檔,避免兩台同步電腦剛好同時按更新而互相覆寫歷史 —— 這純粹是寫入端的
# 防撞機制(Google Drive 同步層擋不住兩台寫同一個檔),不代表資料分屬各機:快照數字
# 一律取自 data/exports/ 的共用後台匯出檔,是全站總量,與哪台電腦執行過檢索無關。
# 所以讀取端(trend)必須合併所有機器的分檔,否則換一台電腦看就少掉別台留下的月份。
_HISTORY_LOCK = threading.Lock()
# ───────────────────────────────────────────────────────


# 匯出檔(假 .xls、真 HTML)解析的行內記憶體快取(依檔案 mtime+size 命中):匯出檔只在
# 使用者手動下載新檔時才變,但原本 compute() 每次都對整份(簡易版常達 ~2MB)重跑正則解析,
# api_breakdown() 又各自再解析一次。改為依簽章快取解析結果:同一份檔只解析一次、跨呼叫
# 與跨頁共用;下載新檔(簽章變)按「更新計算」才會重解析,故永不回傳過時數字。
_TABLE_CACHE = {}


def read_table(path):
    """把『假 .xls、真 HTML 表格』解析成列的清單，每列是欄位字串清單。
    依檔案簽章快取;回傳的 list 僅供讀取、呼叫端不可改動(快取共用同一物件)。"""
    sig = _file_sig(path)
    ent = _TABLE_CACHE.get(path)
    if ent and ent.get("sig") == sig:
        return ent["rows"]
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception:
        return ent["rows"] if ent else []     # 讀不到:沿用舊快取(若有),否則空表
    text = raw.decode("utf-8", errors="replace")
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.I | re.S):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.I | re.S)
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        if cells:
            rows.append(cells)
    _TABLE_CACHE[path] = {"sig": sig, "rows": rows}
    return rows


def to_int(s):
    """'      6,552' -> 6552；非數字 -> 0。"""
    s = re.sub(r"[,\s]", "", s or "")
    return int(s) if s.isdigit() else 0


def col_index(header, name):
    """找欄位索引：先精確比對，再用『包含』模糊比對。"""
    if name in header:
        return header.index(name)
    for i, h in enumerate(header):
        if name in h:
            return i
    return None


def sum_count(path, ym):
    """加總檔案中『檢索次數』欄，只計屬於 ym（YYYYMM）的列。
    回傳 (總和, 採計列數, 略過列數, 偵測到的最後日期字串)。"""
    rows = read_table(path)
    if not rows:
        return 0, 0, 0, ""
    header = rows[0]
    ci = col_index(header, "檢索次數")
    di = col_index(header, "檢索日期")
    if di is None:
        di = col_index(header, "年月")
    if ci is None:
        raise ValueError(f"在 {os.path.basename(path)} 找不到『檢索次數』欄，表頭為：{header}")

    total = used = skipped = 0
    last_date = ""
    for r in rows[1:]:
        if ci >= len(r):
            continue
        if di is not None and di < len(r):
            dval = re.sub(r"\s", "", r[di])
            if dval and not dval.startswith(ym):
                skipped += 1
                continue
            if dval > last_date:
                last_date = dval
        total += to_int(r[ci])
        used += 1
    return total, used, skipped, last_date


def load_holidays():
    d = read_json(HOLIDAYS_PATH, default={})
    if not isinstance(d, dict):
        return set(), set()
    return set(d.get("holidays", [])), set(d.get("makeup_workdays", []))


def load_monthly_target():
    """targets.json 有 monthly_target 就用它，否則用預設常數。"""
    t = read_json(TARGETS_PATH, default={})
    try:
        v = int(t.get("monthly_target", 0) or 0) if isinstance(t, dict) else 0
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else DEFAULT_MONTHLY_TARGET


def is_workday(day, holidays, makeups):
    """指定日期是否為工作日:扣國定假日,週末僅補班日算工作日。"""
    iso = day.isoformat()
    return iso not in holidays and (day.weekday() < 5 or iso in makeups)


def remaining_workdays(today, holidays, makeups):
    """今天（含）到本月底，扣掉週末與國定假日、加回補班日，剩幾個工作日。"""
    y, m = today.year, today.month
    last = calendar.monthrange(y, m)[1]
    return sum(is_workday(datetime.date(y, m, day), holidays, makeups)
               for day in range(today.day, last + 1))


def month_workday_progress(day, holidays, makeups):
    """回傳 (當月工作日總數, 截至指定日已過工作日數),供達標趨勢基準線使用。"""
    last = calendar.monthrange(day.year, day.month)[1]
    all_days = [datetime.date(day.year, day.month, n) for n in range(1, last + 1)]
    workdays = [d for d in all_days if is_workday(d, holidays, makeups)]
    return len(workdays), sum(d <= day for d in workdays)


def latest(pattern):
    """從 data/exports/ 找最新匯出檔。"""
    files = glob.glob(str(EXPORTS_DIR / pattern))
    return max(files, key=os.path.getmtime) if files else None


def load_used_codes():
    """讀 gpss/api_config*.json 內的 userCode = 這套工具實際在用的 API。
    回傳 {小寫驗證碼: 原始驗證碼}(去重;同碼出現在共用與各機設定只算一個)。"""
    used = {}
    cfg = read_json(os.path.join(BASE, "config.json"), default={})
    gpss_dir = (cfg.get("gpss_dir") if isinstance(cfg, dict) else None) or "program/gpss"
    for path in glob.glob(os.path.join(BASE, gpss_dir, "api_config*.json")):
        one = read_json(path, default={})
        uc = str(one.get("userCode", "")).strip() if isinstance(one, dict) else ""
        if uc:
            used[uc.lower()] = uc
    return used


def load_pool_codes():
    """讀 gpss/api_keys.json(驗證碼池)裡的每組 userCode = 這套工具的『各組驗證碼』。
    回傳 {小寫驗證碼: 原始驗證碼}。月用量要列的就是池裡這些碼(含目前沒被選用、但仍屬本
    工具的碼),不是後台所有人的碼。"""
    pool = {}
    cfg = read_json(os.path.join(BASE, "config.json"), default={})
    gpss_dir = (cfg.get("gpss_dir") if isinstance(cfg, dict) else None) or "program/gpss"
    keys = read_json(os.path.join(BASE, gpss_dir, "api_keys.json"), default={})
    for k in (keys.get("keys", []) if isinstance(keys, dict) else []):
        uc = str(k.get("userCode", "")).strip() if isinstance(k, dict) else ""
        if uc:
            pool[uc.lower()] = uc
    return pool


CONFIG_FILE_RE = re.compile(r"api_config_(.+)\.json$")
RUNLOG_FILE_RE = re.compile(r"run_log_(.+)\.jsonl$")


def machine_usercodes():
    """回傳 {機器代號: userCode 小寫} —— 各機 API 檢索實際在用的驗證碼。
    以各機的 api_config_<機器>.json 為準;其 userCode 空白則退回共用
    api_config.json(與 gpss_api_query.load_config 的取碼規則一致)。"""
    cfg = read_json(os.path.join(BASE, "config.json"), default={})
    gpss_dir = (cfg.get("gpss_dir") if isinstance(cfg, dict) else None) or "program/gpss"
    shared = read_json(os.path.join(BASE, gpss_dir, "api_config.json"), default={})
    shared_uc = str(shared.get("userCode", "")).strip() if isinstance(shared, dict) else ""
    out = {}
    for path in glob.glob(os.path.join(BASE, gpss_dir, "api_config_*.json")):
        m = CONFIG_FILE_RE.search(os.path.basename(path))
        if not m:
            continue
        one = read_json(path, default={})
        uc = str(one.get("userCode", "")).strip() if isinstance(one, dict) else ""
        uc = uc or shared_uc
        if uc:
            out[m.group(1)] = uc.lower()
    return out


# run_log 的 API 紀錄行內記憶體快取(依檔案 mtime+size 命中):run_log 只在尾端追加、
# 不重寫,原本每次本月 API 用量計算都把整份重讀並逐行 json.loads(檔案隨月份無限長大)。
# 改為依簽章快取「該檔所有 API 檢索原始紀錄」;月份/下載時刻/碼歸戶等過濾仍在讀取時即時做
# (這些參數每次呼叫可能不同,不能併進快取)。程序常駐,跨請求重用。
_API_RUNLOG_CACHE = {}


def _api_recs_for_file(path, file_mid):
    """解析單一 run_log 檔,回傳其中所有『API 檢索且 queries>0』的紀錄:
    [(date, ts, queries, code_lower, mid), ...]。依簽章快取;讀不到檔時沿用舊快取(若有)。"""
    sig = _file_sig(path)
    ent = _API_RUNLOG_CACHE.get(path)
    if ent and ent.get("sig") == sig:
        return ent["recs"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        return ent["recs"] if ent else []
    recs = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        # 只計 API 檢索(傳統前端檢索不耗 API 額度)。新紀錄有 method 欄;
        # 早於本功能的舊 API 紀錄沒有 method,但有只有 API 版才寫的 "ap" 欄可辨識。
        is_api = rec.get("method") == "api" or ("ap" in rec and "method" not in rec)
        if not is_api:
            continue
        q = int(rec.get("queries", 0) or 0)
        if not q:
            continue
        recs.append((rec.get("date") or "", rec.get("ts") or "", q,
                     str(rec.get("code", "")).strip().lower(),
                     rec.get("machine") or file_mid))
    _API_RUNLOG_CACHE[path] = {"sig": sig, "recs": recs}
    return recs


def api_runlog_since(ym, after_ts):
    """掃 data/run_log*.jsonl,加總『method==api』且屬本月、時間戳晚於 after_ts 的
    檢索筆數,**依該次執行實際用的驗證碼分組**。after_ts 為 'YYYY-MM-DD HH:MM:SS'
    (匯出檔下載時刻;空字串 = 不設下限,全本月都算)。這就是「匯出檔下載之後、本工具
    自己跑掉的 API 檢索」,用來把後台匯出的用量補到即時。
    用『下載時刻』而非『資料日期』當界線,才能把『下載後同一天』又跑的也算進來
    (用資料日期會把整個下載當天都排除掉)。

    歸戶以紀錄裡的 `code`(執行當下寫死的 userCode)為準 —— 中途切換驗證碼也分帳正確;
    舊紀錄沒有 code 欄,才退回『該機器目前選的碼』(machine_usercodes)。
    回傳 {驗證碼小寫: 檢索筆數}。"""
    mid_to_uc = machine_usercodes()         # 退路:舊紀錄沒記碼時,用機器目前選的碼
    out = {}
    for path in glob.glob(str(MACHINE_DATA_DIR / "run_log*.jsonl")):
        m = RUNLOG_FILE_RE.search(os.path.basename(path))
        file_mid = m.group(1) if m else None
        for date, ts, q, code, mid in _api_recs_for_file(path, file_mid):
            if date.replace("-", "")[:6] != ym:           # 只計本月
                continue
            if after_ts and (not ts or ts <= after_ts):    # 只計下載時刻之後(避免與匯出檔重複)
                continue
            if not code:                        # 舊紀錄沒記碼 → 退回機器目前選的碼
                code = mid_to_uc.get(mid, "")
            if code:
                out[code] = out.get(code, 0) + q
    return out


def api_breakdown(today=None):
    """讀最新 API 匯出檔,依 API驗證碼 加總『本月』檢索次數,**依驗證碼歸戶**:
    凡是本月有用量的碼都列出 —— 後台匯出檔出現過的、或本工具跑過的、或目前設定在用的
    (聯集),不再因為『某碼現在沒被任何機器選用』就把它的本月用量藏起來(那會讓切換過的
    舊碼看起來像 0)。
    再以匯出檔資料日期為界,把『匯出後本工具自己跑掉的 API 檢索』即時補上(依執行當下
    記錄的碼分帳),讓用量不必重新下載匯出檔也能反映最新。
    回傳 {"codes": [{"code": 遮罩後, "count": int}, ...](由多到少),
          "total": 這些 API 的總次數, "data_date": 最後日期,
          "added": 匯出後本工具補加的筆數}。"""
    today = today or datetime.date.today()
    ym = today.strftime("%Y%m")
    used = load_used_codes()           # {lower: original} 目前設定在用的碼
    pool = load_pool_codes()           # {lower: original} 驗證碼池裡的各組碼(= 本工具的碼)
    ours = {}                          # {lower: 原始碼} 本工具的碼(池 ∪ 設定),供遮罩顯示
    ours.update(pool)
    ours.update(used)
    orig_of = dict(ours)               # 遮罩顯示用;export 出現的原碼下面再補(只補我們自己的)

    f_api = latest(API_GLOB)
    rows = read_table(f_api) if f_api else []
    header = rows[0] if rows else []
    ci = col_index(header, "檢索次數")
    code_i = col_index(header, "API驗證碼")
    if code_i is None:
        code_i = col_index(header, "驗證碼")
    di = col_index(header, "檢索日期")

    agg, last_date = {}, ""            # {lower: 本月次數}
    have_export = bool(rows and ci is not None and code_i is not None)
    if have_export:
        for r in rows[1:]:
            if ci >= len(r) or code_i >= len(r):
                continue
            if di is not None and di < len(r):
                dval = re.sub(r"\s", "", r[di])
                if dval and not dval.startswith(ym):
                    continue
                if dval > last_date:
                    last_date = dval
            code = r[code_i].strip()
            if not code:
                continue
            low = code.lower()
            agg[low] = agg.get(low, 0) + to_int(r[ci])
            if low in ours:                     # 只記我們自己的碼的原碼(後台別人的碼不顯示)
                orig_of.setdefault(low, code)

    # 匯出後本工具自己跑的 API 檢索,即時補上 —— api_runlog_since 已依執行當下記錄的碼分組。
    # 界線用『匯出檔下載時刻(mtime)』:匯出檔內含的是下載當下後台已有的用量,
    # 下載後再跑的(含同一天)其 ts 必晚於 mtime → 不會與匯出檔重複,也不會漏掉同日。
    #   ‧ 沒有匯出檔 → after_ts 留空,本月全部 run_log API 都補。
    after_ts = ""
    if f_api:
        try:
            after_ts = datetime.datetime.fromtimestamp(
                os.path.getmtime(f_api)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            after_ts = ""
    extra = api_runlog_since(ym, after_ts)      # {lower: 匯出後新增筆數}(已依碼分組)
    for low in extra:
        orig_of.setdefault(low, low)            # 本工具跑過、但不在池/設定裡的碼也算我們的

    # 只列『本工具的碼』:驗證碼池 ∪ 目前設定 ∪ 本月本工具跑過的碼。匯出檔只用來查每碼的
    # 本月次數(後台其他人的碼不在此列)。池裡本月還沒用過的碼會以 0 列出。
    our_codes = set(ours) | set(extra)
    codes = [{"code": mask_code(orig_of.get(low, low)),
              "count": agg.get(low, 0) + extra.get(low, 0)}
             for low in our_codes]
    codes.sort(key=lambda x: x["count"], reverse=True)
    return {"codes": codes, "total": sum(c["count"] for c in codes),
            "data_date": last_date, "added": sum(extra.values())}


def basis_day(data_date, ym, today):
    """進度估算的『基準日』:用資料最後日期(YYYYMMDD,且屬本月 ym)當基準,
    取不到(沒資料/跨月)就退回系統當天。

    為什麼用資料日期而非系統當天:匯出檔可能隔幾天才下載一次。若用系統當天當
    分母(總量 ÷ 今天是幾號),沒下載的那幾天 today 一直增加、total 卻沒變,
    日均會被低估、建議筆數每天往上飄。改用資料日期後,沒換新檔數字就不動,
    下載新檔(資料日期前進)才更新。"""
    s = re.sub(r"\D", "", data_date or "")
    if len(s) == 8 and s[:6] == ym:
        try:
            return datetime.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            pass
    return today


def compute(today=None):
    """計算本月進度，回傳給 CLI 與網頁共用的 dict。"""
    today = today or datetime.date.today()
    ym = today.strftime("%Y%m")
    holidays, makeups = load_holidays()
    target = load_monthly_target()

    f_simple = latest(SIMPLE_GLOB)
    f_api = latest(API_GLOB)

    simple_total = simple_rows = 0
    simple_file = simple_last = ""
    if f_simple:
        simple_total, simple_rows, _, simple_last = sum_count(f_simple, ym)
        simple_file = os.path.basename(f_simple)

    api_total = api_rows = 0
    api_file = api_last = ""
    if f_api:
        api_total, api_rows, _, api_last = sum_count(f_api, ym)
        api_file = os.path.basename(f_api)

    total = simple_total + api_total
    remaining = max(0, target - total)          # 距目標的原始缺口

    # 資料新鮮度：取 API 的最後日期(YYYYMMDD)較精細；沒有就用簡易版的年月
    data_date = api_last or simple_last
    # 估算一律以「資料最後日期」為基準日(而非系統當天),沒下載新匯出檔就不會每天飄。
    cday = basis_day(data_date, ym, today)
    wd = remaining_workdays(cday, holidays, makeups)

    days_in_month = calendar.monthrange(cday.year, cday.month)[1]
    days_left = days_in_month - cday.day         # 基準日之後的剩餘日曆天
    # 日均增量(含民眾使用,用本月累積 ÷ 基準日的天數估)→ 推估到月底會自然累積到多少。
    daily_avg = round(total / cday.day) if cday.day > 0 else 0
    projected = total + daily_avg * days_left    # 照目前速度,月底自然累積
    natural_left = max(0, projected - total)     # 基準日到月底會自然增加的量
    crawl_needed = max(0, target - projected)    # 扣掉自然增加後,我實際還需檢索
    per_workday = -(-crawl_needed // wd) if wd > 0 else 0   # 無條件進位
    # 資料更新時間 = 匯出檔本身的最後修改時間(沒換檔就不會變;比按鈕當下時間有意義)。
    mtimes = [os.path.getmtime(p) for p in (f_simple, f_api) if p]
    data_mtime = (datetime.datetime.fromtimestamp(max(mtimes)).strftime("%Y-%m-%d %H:%M")
                  if mtimes else "")

    return {
        "ok": True,
        "month": today.strftime("%Y-%m"),
        "monthly_target": target,
        "simple_total": simple_total,
        "simple_file": simple_file,
        "simple_rows": simple_rows,
        "api_total": api_total,
        "api_file": api_file,
        "api_rows": api_rows,
        "total": total,
        "remaining": remaining,
        "percent": (total / target * 100) if target > 0 else 0,
        "workdays_remaining": wd,
        "daily_avg": daily_avg,
        "natural_left": natural_left,
        "projected": projected,
        "crawl_needed": crawl_needed,
        "per_workday": per_workday,
        "on_track": projected >= target,
        "data_date": data_date,
        "data_mtime": data_mtime,
        "files_found": bool(f_simple or f_api),
    }


def _read_history_file(path) -> list:
    """讀單一份快照檔;Google Drive 同步暫時讀不到或壞檔時安全退回空紀錄。"""
    items = read_json(path, default=[])
    return items if isinstance(items, list) else []


def _load_history() -> list:
    """讀本機那份快照檔。只給寫入端用:附加新快照時不該把別台的紀錄抄進自己的檔。"""
    return _read_history_file(HISTORY_PATH)


def _load_all_history() -> list:
    """讀所有機器的快照檔並合併,供達標分析顯示。

    分檔只是寫入防撞,快照內容是共用匯出檔的全站總量,故合併後即為完整歷史。
    壞掉或讀不到的單一檔案略過即可,不讓一台的問題把整張圖清空。
    """
    items = []
    for path in sorted(MACHINE_DATA_DIR.glob("monthly_progress_history_*.json")):
        items.extend(_read_history_file(path))
    return items


def _save_history(items: list) -> None:
    """原子寫入快照歷史;同步磁碟短暫忙碌時有限重試(write_json_atomic 預設重試 4 次)。"""
    write_json_atomic(HISTORY_PATH, items)


def record_snapshot(progress=None, now=None):
    """留存一次使用者主動更新的月進度快照;不回推本功能啟用前的任何歷史資料。"""
    progress = progress or compute()
    if not progress.get("ok") or not progress.get("files_found"):
        return None
    now = now or datetime.datetime.now()
    today = now.date()
    holidays, makeups = load_holidays()
    total_workdays, elapsed_workdays = month_workday_progress(today, holidays, makeups)
    target = int(progress.get("monthly_target", 0) or 0)
    target_to_date = round(target * elapsed_workdays / total_workdays) if total_workdays else 0
    snap = {
        "captured_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "machine": MACHINE_ID,
        "month": progress.get("month", today.strftime("%Y-%m")),
        "total": int(progress.get("total", 0) or 0),
        # 分開保存兩種檢索方式；此欄位從分類圖表功能啟用後才有，絕不回填舊快照。
        "simple_total": int(progress.get("simple_total", 0) or 0),
        "api_total": int(progress.get("api_total", 0) or 0),
        "monthly_target": target,
        "percent": round(float(progress.get("percent", 0) or 0), 3),
        "projected": int(progress.get("projected", 0) or 0),
        "on_track": bool(progress.get("on_track")),
        "per_workday": int(progress.get("per_workday", 0) or 0),
        "data_date": progress.get("data_date", ""),
        "workdays_elapsed": elapsed_workdays,
        "workdays_total": total_workdays,
        "target_to_date": target_to_date,
    }
    with _HISTORY_LOCK:
        items = _load_history()
        items.append(snap)
        _save_history(items)
    return snap


def trend(month=None):
    """回傳功能啟用後留存的指定月份快照,供三種趨勢圖使用。

    圖表 X 軸代表輸入匯出檔的「資料最後日期」(data_date)，不是按更新按鈕的時間。
    同一資料日期可能重複按更新(含在不同電腦上各按過)；那不是新數據，故只保留該資料日期
    最後一次的快照。
    """
    with _HISTORY_LOCK:
        all_items = _load_all_history()
    months = sorted({str(x.get("month", "")) for x in all_items if x.get("month")}, reverse=True)
    month = month or datetime.date.today().strftime("%Y-%m")
    snapshots = [x for x in all_items if x.get("month") == month]
    snapshots.sort(key=lambda x: x.get("captured_at", ""))
    by_data_date = {}
    for snap in snapshots:
        # data_date 是匯出資料到哪一天；舊快照少了它才退回其建立日，避免無法顯示。
        raw = re.sub(r"\D", "", str(snap.get("data_date", "")))[:8]
        if len(raw) != 8:
            raw = re.sub(r"\D", "", str(snap.get("captured_at", "")))[:8]
        if raw:
            by_data_date[raw] = snap
    points = []
    for raw, snap in sorted(by_data_date.items()):
        point = dict(date=f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}", **snap)
        point["simple_available"] = "simple_total" in point
        point["api_available"] = "api_total" in point
        points.append(point)
    latest = snapshots[-1] if snapshots else None
    return {"month": month, "months": months, "snapshots": snapshots, "points": points,
            "snapshot_count": len(snapshots), "latest": latest}


def available_trend_months() -> list:
    """保留給 HTTP 層或其他呼叫端的月份清單捷徑。"""
    return trend().get("months", [])


def fmt(n):
    return f"{n:,}"


def main():
    r = compute()
    print("=" * 56)
    print(f"  月檢索進度  —  {datetime.date.today().isoformat()}（本月 {r['month'].replace('-', '')}）")
    print("=" * 56)

    if r["simple_file"]:
        print(f"簡易版檔：{r['simple_file']}")
        print(f"   檢索次數加總 = {fmt(r['simple_total'])}（採計 {r['simple_rows']} 列）")
    else:
        print(f"[注意] 找不到簡易版檔（樣式 {SIMPLE_GLOB}），請下載後放進 data/exports/")

    if r["api_file"]:
        print(f"API 檔：{r['api_file']}")
        print(f"   檢索次數加總 = {fmt(r['api_total'])}（採計 {r['api_rows']} 列，資料到 {r['data_date']}）")
    else:
        print(f"[注意] 找不到 API 檔（樣式 {API_GLOB}），請下載後放進 data/exports/")

    print("-" * 56)
    print(f"本月累積（簡易版 + API） = {fmt(r['total'])}  /  月目標 {fmt(r['monthly_target'])}")
    print(f"月底自然累積（推估）     = {fmt(r['projected'])} 筆")

    if r["on_track"]:
        print("[趨勢達標] 照目前速度月底即可達標，不需額外加爬。")
    else:
        print(f"扣掉自然累積後，我還需檢索 = {fmt(r['crawl_needed'])} 筆")
        print(f"本月剩餘工作日（含今天）   = {r['workdays_remaining']} 天")
        if r["workdays_remaining"] > 0:
            print(f">> 平均每個工作日需檢索    約 {fmt(r['per_workday'])} 筆")
        else:
            print("[注意] 本月已無剩餘工作日。")

    print("=" * 56)
    print("註：僅含簡易版+API，且含民眾使用，屬偏保守（偏低）估計。")


if __name__ == "__main__":
    main()
