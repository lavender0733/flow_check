# -*- coding: utf-8 -*-
"""
output_store.py — 把 output/ 下零散的 summary_*.csv 併進「每台主機、每個月一個 Excel」
================================================================================

目標:output 資料夾不要再塞滿一堆 CSV,而且彙整檔不要無限長大。改成:
    output/summary_<機器>_<YYYY-MM>.xlsx          (一台主機、一個月一個檔)
裡面**一天一張工作表**(工作表名 = 日期,如 2026-06-09);同一天不同時間執行的
記錄,都附加到當天那張工作表;新的一天就自動多一張工作表;**跨月就自動換新檔**。

為什麼要按月分檔(本次優化重點):
  舊版是「每台一個 summary_<機器>.xlsx、無限累積」。每次 consolidate 都要 load 整份檔
  → append → save 整份檔,檔案越大、寫入越慢、Google Drive 同步越容易撞鎖/出半截檔;
  統計每次刷新也要把整份歷史重讀一遍。改成按月分檔後:**寫入只碰「當月」那個小檔**,
  過去月份的檔完全不再被開啟/重寫,大小天然封頂在一個月。

為什麼還留 CSV:檢索程式仍逐列寫 CSV(每列即時 flush),所以中途被「立即停止」
硬殺也不會掉資料。consolidate() 在每次檢索的前/後,把**已完成的 CSV**併進對應月份的
Excel 並刪掉該 CSV —— Excel 是彙整結果,CSV 只是過渡。需要 openpyxl;沒有就略過彙整、
維持原本純 CSV 的行為(統計仍讀得到)。

統計讀取另加「已結算日彙總快取」(_daily_cache.json):每個月份檔依 mtime+size 簽章,
簽章沒變就直接用上次算好的每日彙總,不再重新解析整份 Excel。只有「當月」那個檔會因為
持續寫入而簽章改變、需要重算;過去月份簽章固定 → 永遠命中快取。

欄位對齊用「表頭名稱」判斷(status / hits / bytes),所以累積自不同版本、欄位順序
不一樣的舊 CSV(有的有 screenshot 欄、有的沒有 hits/bytes 欄)都能正確併入、不錯位。
工作表欄位統一為:time, idx, patent, status, hits, bytes(time = 該筆所屬執行的開跑時刻)。
"""

import re
import csv
import tempfile
from pathlib import Path

from common.ident import machine_id
from common.jsonio import file_sig as _sig, read_json, write_json_atomic

try:
    from openpyxl import Workbook, load_workbook
except Exception:                       # 沒裝 openpyxl 就降級:不彙整、只用 CSV
    Workbook = None
    load_workbook = None

BASE = Path(__file__).resolve().parent
OUTPUT_DIR = BASE / "output"
CSV_RE = re.compile(r"summary_(\d{8})_(\d{6})(?:_(.+))?\.csv$")   # 日期, 時間, 機器(可選)
# 新版檔名:summary_<機器>_<YYYY-MM>.xlsx。`.+` 貪婪、月份錨在結尾,故機器代號帶連字號
# (如 DESKTOP-V33GG01)也切得對。Google Drive 衝突複本(summary_<機器>_2026-06 (1).xlsx)
# 因結尾不是純 \d{4}-\d{2} 而整個比不中 → 自動跳過。
XLSX_MONTH_RE = re.compile(r"summary_(.+)_(\d{4}-\d{2})\.xlsx$")
# 舊版單檔(尚未遷移):summary_<機器>.xlsx,沒有月份段。
XLSX_OLD_RE = re.compile(r"summary_(.+)\.xlsx$")
# 機器代號一律由 machine_id() 過濾成 [A-Za-z0-9-],絕不含空白或括號。
# Google Drive 同步衝突時會把檔名改成「summary_<機器> (1).xlsx」之類,捕到的機器
# 代號就會帶空白/括號 —— 用這個比對把那種衝突複本擋掉,否則會被當成「多一台機器」
# 重複計數(統計筆數整個翻倍灌水)。
MID_RE = re.compile(r"^[A-Za-z0-9-]+$")
# code = 該次執行實際使用的 API 驗證碼(userCode);傳統檢索 web 記 "web"(不耗 API 額度)。
# 額度統計改以這個「執行當下寫死的碼」歸戶,而非事後查機器目前選哪組 —— 中途切換驗證碼
# 也能正確分帳。舊資料沒有這欄(讀回為空/None),呼叫端會退回舊的「機器→目前碼」對應。
HEADER = ["time", "idx", "patent", "status", "hits", "bytes", "code"]
MERGED_SHEET = "_merged"     # 隱藏工作表:記已併入的 CSV 檔名,確保不重複併入/不重複計數
CACHE_FILE = "_daily_cache.json"   # 統計用的「已結算日彙總」快取(依各月份檔簽章命中)

# ok_runs / ok_by_case 共用的「OK 列」行內記憶體快取(依檔案 mtime+size 簽章命中):
# 過去月份檔簽章固定 → 永遠命中;只有當月檔會因持續寫入而重算。兩者都只看 OK 列,故
# 共用同一份解析 —— 單次統計請求不再為「時段額度卡」與「單筆案號卡」各開一次整份 Excel
# (load_workbook 解 zip/XML 是整條路徑最貴的動作)。程序常駐,跨請求重用。
_OK_ROWS_CACHE = {}


def _atomic_save(wb, path):
    """存成同資料夾的暫存檔再 replace() 換上去。openpyxl 的 wb.save() 是直接覆寫目標檔、
    不是原子的:存到一半若程序被砍(例如使用者關掉 app),原本好好的 Excel 會被截成半截
    /壞檔。改成先寫 <檔名>.tmp、寫完才 replace() 一次換上 —— Google Drive 同步夾上尤其
    要這樣(replace 在同一磁碟是原子的,絕不會留下半截的當月彙整檔)。"""
    tmp = path.with_name(path.name + ".tmp")
    wb.save(tmp)
    tmp.replace(path)


def _is_conflict_mid(mid: str) -> bool:
    """mid 不是合法機器代號 → 視為 Google Drive 衝突複本/異常檔名,統計與彙整都跳過。"""
    return not MID_RE.match(mid or "")


def _sheet_for(d8: str) -> str:         # 20260609 -> 2026-06-09(合法的工作表名)
    return f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}"


def _month_of_d8(d8: str) -> str:       # 20260609 -> 2026-06
    return f"{d8[:4]}-{d8[4:6]}"


def _parse_xlsx_name(name: str):
    """解析彙整 Excel 檔名 → (mid, month) 或 (mid, None)(舊單檔),非法/衝突複本回 None。
    新版 summary_<機器>_<YYYY-MM>.xlsx → (機器, '2026-06');
    舊版 summary_<機器>.xlsx → (機器, None)。"""
    m = XLSX_MONTH_RE.match(name)
    if m:
        mid = m.group(1)
        return (mid, m.group(2)) if MID_RE.match(mid) else None
    m = XLSX_OLD_RE.match(name)
    if m:
        mid = m.group(1)                 # 新版檔名的 mid 會含底線('PC_2026-06'),MID_RE 擋掉
        return (mid, None) if MID_RE.match(mid) else None
    return None


def _summary_xlsx_files(out) -> list:
    """回傳要讀的彙整 Excel:[(path, mid, month_or_None), ...],已濾掉衝突複本。
    防雙重計數:某機若已有月份檔,該機的舊單檔(month=None)就跳過 —— 遷移時若「月份檔
    已建好、舊單檔還沒改成 .bak」這個半途狀態下,讀取仍只算月份檔,不會兩邊都算。"""
    files = []
    have_month = set()                   # 已有月份檔的機器
    for p in out.glob("summary_*.xlsx"):
        info = _parse_xlsx_name(p.name)
        if not info:
            continue
        mid, month = info
        files.append((p, mid, month))
        if month is not None:
            have_month.add(mid)
    return [(p, mid, mon) for (p, mid, mon) in files
            if not (mon is None and mid in have_month)]


def _as_int(v, default=0):
    try:
        return int(str(v).strip())
    except (ValueError, TypeError, AttributeError):
        return default


def _col_map(header) -> dict:
    """依表頭找各欄索引(先精確比對,再用『包含』)。找不到的欄位不放進結果。"""
    h = [str(x).strip().lower() for x in (header or [])]
    cm = {}
    for name in ("idx", "patent", "status", "hits", "bytes", "code"):
        if name in h:
            cm[name] = h.index(name)
        else:
            for i, col in enumerate(h):
                if name in col:
                    cm[name] = i
                    break
    return cm


def _read_csv_rows(path):
    """讀一個 summary CSV,依表頭把欄位對齊,回傳
    [(idx, patent, status, hits, bytes_int, code), ...](不論來源欄位順序為何都正確)。
    code = 該次執行用的 userCode(舊檔沒有此欄 → 空字串,呼叫端退回機器對應)。"""
    out = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rdr = csv.reader(f)
        cm = _col_map(next(rdr, None))
        ii, pi, si = cm.get("idx"), cm.get("patent"), cm.get("status")
        hi, bi, ci = cm.get("hits"), cm.get("bytes"), cm.get("code")

        def g(row, i):
            return row[i] if (i is not None and i < len(row)) else ""

        for row in rdr:
            if not row:
                continue
            out.append((_as_int(g(row, ii), g(row, ii)), g(row, pi), g(row, si),
                        _as_int(g(row, hi), g(row, hi)), _as_int(g(row, bi)),
                        str(g(row, ci) or "").strip()))
    return out


def _merged_set(wb) -> set:
    """讀 Excel 的 _merged 工作表,回傳已併入過的 CSV 檔名集合。"""
    s = set()
    if MERGED_SHEET in wb.sheetnames:
        for row in wb[MERGED_SHEET].iter_rows(values_only=True):
            if row and row[0]:
                s.add(str(row[0]))
    return s


# ---------------------------------------------------------------------------
# 一次性遷移:舊單檔 summary_<機器>.xlsx → 按月份拆成 summary_<機器>_<YYYY-MM>.xlsx
# ---------------------------------------------------------------------------
def migrate_split_monthly(output_dir=None) -> int:
    """把舊的單檔 summary_<機器>.xlsx 依工作表日期拆成月份檔;原檔改名加 .bak 保留為備份。
    冪等:遷移後舊檔變 .bak、不再被偵測;沒有舊單檔時不動作。回傳遷移的舊檔數。"""
    if Workbook is None:
        return 0
    out = Path(output_dir) if output_dir else OUTPUT_DIR
    if not out.exists():
        return 0

    done = 0
    for p in list(out.glob("summary_*.xlsx")):
        info = _parse_xlsx_name(p.name)
        if not info:
            continue
        mid, month = info
        if month is not None:            # 已是月份檔 → 不是遷移對象
            continue

        try:
            src = load_workbook(p, read_only=True, data_only=True)
        except Exception:
            continue                     # 壞檔/被鎖,下次再試
        per_month = {}                   # month -> {date: [rows...]}(rows 含表頭那列)
        merged_by_month = {}             # month -> [csv 檔名]
        try:
            for ws in src.worksheets:
                if ws.title == MERGED_SHEET:
                    for row in ws.iter_rows(values_only=True):
                        if not (row and row[0]):
                            continue
                        nm = str(row[0])
                        mm = CSV_RE.search(nm)
                        if mm:
                            merged_by_month.setdefault(_month_of_d8(mm.group(1)), []).append(nm)
                    continue
                date = str(ws.title)
                mon = date[:7]           # 'YYYY-MM-DD' -> 'YYYY-MM'
                rows = [list(r) for r in ws.iter_rows(values_only=True)]
                per_month.setdefault(mon, {})[date] = rows
        except Exception:
            try:
                src.close()
            except Exception:
                pass
            continue                     # 半截/損毀:整檔放棄遷移(原檔留著,下次再試)
        try:
            src.close()
        except Exception:
            pass

        ok = True
        for mon, sheets in per_month.items():
            xlsx = out / f"summary_{mid}_{mon}.xlsx"
            new_file = not xlsx.exists()
            try:
                wb = Workbook() if new_file else load_workbook(xlsx)
            except Exception:
                ok = False
                continue
            for date, rows in sheets.items():
                if date in wb.sheetnames:    # 該日已在月份檔(consolidate 已寫過)→ 不重複
                    continue
                ws = wb.create_sheet(title=date)
                for r in rows:
                    ws.append(r)
            names = merged_by_month.get(mon, [])
            if names:
                mws = wb[MERGED_SHEET] if MERGED_SHEET in wb.sheetnames \
                    else wb.create_sheet(title=MERGED_SHEET)
                mws.sheet_state = "hidden"
                for nm in names:
                    mws.append([nm])
            if new_file and "Sheet" in wb.sheetnames and len(wb.sheetnames) > 1:
                try:
                    wb.remove(wb["Sheet"])
                except Exception:
                    pass
            try:
                wb._sheets.sort(key=lambda s: s.title)
            except Exception:
                pass
            try:
                _atomic_save(wb, xlsx)
            except Exception:
                ok = False

        if ok:                           # 全部月份都寫好了,才把舊檔降級為備份
            bak = out / (p.name + ".bak")
            try:
                if bak.exists():
                    bak.unlink()
                p.rename(bak)
                done += 1
            except Exception:
                pass                     # 改名失敗:舊檔留著,讀取端仍會把它當舊單檔讀到
    return done


def consolidate(only_machine: str = None, output_dir=None) -> int:
    """把 output/ 完成的 summary_*.csv 併進對應月份的 summary_<機器>_<YYYY-MM>.xlsx,
    併入後刪 CSV。每次只開「該 CSV 所屬月份」那個檔,過去月份檔完全不碰。

    以 Excel 內的 _merged 工作表記下「已併入的 CSV 檔名」:已併入過的不會再併第二次
    (即使上次刪檔失敗、檔還在),統計也不會重複算。所以對 Google Drive 偶發的檔案
    鎖定是安全的 —— 刪不掉頂多檔案多留一下,下次再刪,絕不重算或重複併入。

    only_machine 指定時只處理該機(檢索程式只併自己的,避免多機同步時寫到別台 Excel)。
    回傳本次新併入的 CSV 檔數;openpyxl 不在時回 0(不動作)。
    """
    if Workbook is None:
        return 0
    out = Path(output_dir) if output_dir else OUTPUT_DIR
    if not out.exists():
        return 0

    migrate_split_monthly(out)           # 確保舊單檔先拆成月份檔(冪等;沒舊檔即無動作)

    groups = {}                          # (mid, month) -> [(path, d8, hms), ...]
    for p in sorted(out.glob("summary_*.csv")):
        m = CSV_RE.search(p.name)
        if not m:
            continue
        d8, hms, mid = m.group(1), m.group(2), (m.group(3) or "PC")
        if _is_conflict_mid(mid):
            continue                     # 衝突複本 CSV,別併進 Excel(會建出假機器/重複)
        if only_machine and mid != only_machine:
            continue
        groups.setdefault((mid, _month_of_d8(d8)), []).append((p, d8, hms))

    merged = 0
    for (mid, month), items in groups.items():
        xlsx = out / f"summary_{mid}_{month}.xlsx"
        new_file = not xlsx.exists()
        try:
            wb = Workbook() if new_file else load_workbook(xlsx)
        except Exception:
            continue                     # 該檔壞了/被鎖,下次再試,先不動 CSV
        already = _merged_set(wb)

        newly = []                       # 本次新併入的 path
        stale = []                       # 先前已併入、只是還沒刪掉的 path(直接清掉)
        for p, d8, hms in items:
            if p.name in already:
                stale.append(p)
                continue
            sheet = _sheet_for(d8)
            try:
                rows = _read_csv_rows(p)
                if sheet in wb.sheetnames:
                    ws = wb[sheet]
                else:
                    ws = wb.create_sheet(title=sheet)
                    ws.append(HEADER)
                t = f"{hms[:2]}:{hms[2:4]}:{hms[4:6]}"
                for (idx, patent, status, hits, b, code) in rows:
                    ws.append([t, idx, patent, status, hits, b, code])
                newly.append(p)
            except Exception:
                continue                 # 這個 CSV 併入失敗就跳過、保留它

        if new_file and "Sheet" in wb.sheetnames and len(wb.sheetnames) > 1:
            try:
                wb.remove(wb["Sheet"])   # 移除 Workbook() 自帶的空白預設工作表
            except Exception:
                pass

        if newly:                        # 把本次併入的檔名登記到 _merged
            if MERGED_SHEET in wb.sheetnames:
                mws = wb[MERGED_SHEET]
            else:
                mws = wb.create_sheet(title=MERGED_SHEET)
                mws.sheet_state = "hidden"
            for p in newly:
                mws.append([p.name])

        if newly:                        # 有新資料才需要重存(只清舊檔則不必)
            try:
                wb._sheets.sort(key=lambda s: s.title)   # 工作表依日期排序(_merged 在最後)
            except Exception:
                pass
            try:
                _atomic_save(wb, xlsx)
            except Exception:
                continue                 # 存檔失敗就不刪 CSV(資料還在 CSV 裡)

        for p in newly + stale:          # 確定存進 Excel 了才刪 CSV(刪不掉下次再刪)
            try:
                p.unlink()
            except Exception:
                pass
        merged += len(newly)
    return merged


# ---------------------------------------------------------------------------
# 統計讀取的快取:每個月份檔依 mtime+size 簽章,簽章沒變就用上次算好的每日彙總
# ---------------------------------------------------------------------------
def _cache_file() -> Path:
    """快取改放『本機暫存』,不再放進雲端同步的 output/ 資料夾。
    它依本機檔案 mtime/size 簽章,本來就不該跨機共用;舊版放在 output/ 會被 daily_counts
    每隔幾秒重寫一次 → Google Drive 一直上傳/下載、還產生衝突複本(_daily_cache (1).json),
    排擠到真正資料檔的同步頻寬。改放本機暫存後:各機各自一份、不進同步、壞了自動重建。"""
    try:
        d = Path(tempfile.gettempdir()) / "flow_check"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"daily_cache_{machine_id()}.json"
    except Exception:
        return Path(tempfile.gettempdir()) / f"flow_check_{CACHE_FILE}"


def _purge_legacy_cache(out):
    """清掉舊版殘留在同步資料夾裡的快取檔(含 Google Drive 衝突複本),讓它不再佔用同步。
    新位置改在本機暫存;此清理只在第一次掃到時真的刪到,之後沒檔可刪即空轉。"""
    try:
        for p in out.glob("_daily_cache*.json*"):
            try:
                p.unlink()
            except Exception:
                pass
    except Exception:
        pass


def _load_cache(out=None) -> dict:
    cache = read_json(_cache_file(), default={})
    return cache if isinstance(cache, dict) else {}


def _save_cache(out=None, cache: dict = None):
    try:
        write_json_atomic(_cache_file(), cache or {})
    except Exception:
        pass                             # 純加速用,寫不成不影響正確性


def _parse_xlsx_counts(p):
    """解析一個彙整 Excel → ({date: [queries, bytes]}, [已併入 CSV 檔名])。
    筆數只計 OK / 查無;失敗不算筆數但流量仍計入。半截/損毀檔回 None(呼叫端跳過)。"""
    try:
        wb = load_workbook(p, read_only=True, data_only=True)
    except Exception:
        return None
    # openpyxl 惰性解析:真正讀 zip/XML 在 iter_rows() 期間,Google Drive 同步到一半的
    # 半截檔會在這裡丟 EOFError/BadZipFile,故整段包 try/except,整檔讀完才回傳。
    try:
        days = {}
        names = []
        for ws in wb.worksheets:
            if ws.title == MERGED_SHEET:
                for row in ws.iter_rows(values_only=True):
                    if row and row[0]:
                        names.append(str(row[0]))
                continue
            q = b = 0
            first = True
            for row in ws.iter_rows(values_only=True):
                if first:
                    first = False
                    continue
                if not row or len(row) < 6:
                    continue
                status = str(row[3] or "")
                if status.startswith("OK") or status == "NO_RESULT":
                    q += 1
                b += _as_int(row[5])
            if q or b:
                days[str(ws.title)] = [q, b]
        return days, names
    except Exception:
        return None
    finally:
        try:
            wb.close()
        except Exception:
            pass


def _parse_xlsx_ok_rows(p):
    """解析一個彙整 Excel → ([(date, run_ts, patent, code), ...只含 OK 列], [已併入 CSV 檔名])。
    供 ok_runs / ok_by_case 共用 —— 兩者都只看 OK 列(status 以 'OK' 開頭),差別只在彙整方式。
    run_ts = 'YYYY-MM-DD HH:MM:SS'(該列所屬執行的開跑時刻)。半截/損毀檔回 None(呼叫端跳過)。"""
    try:
        wb = load_workbook(p, read_only=True, data_only=True)
    except Exception:
        return None
    # iter_rows() 才真正解析 zip/XML;同步到一半的半截檔會在此丟 EOFError/BadZipFile,
    # 故整段迭代包 try/except,整檔讀完才回傳(避免計入半截數據)。
    try:
        rows = []
        names = []
        for ws in wb.worksheets:
            if ws.title == MERGED_SHEET:
                for row in ws.iter_rows(values_only=True):
                    if row and row[0]:
                        names.append(str(row[0]))
                continue
            first = True
            for row in ws.iter_rows(values_only=True):
                if first:                            # 跳過表頭
                    first = False
                    continue
                if not row or len(row) < 6:
                    continue
                if not str(row[3] or "").startswith("OK"):
                    continue
                t = str(row[0] or "").strip()        # 開跑時刻 HH:MM:SS
                code = str(row[6]).strip() if len(row) > 6 and row[6] is not None else ""
                rows.append((ws.title, f"{ws.title} {t or '00:00:00'}",
                             str(row[2] or "").strip(), code))
        return rows, names
    except Exception:
        return None
    finally:
        try:
            wb.close()
        except Exception:
            pass


def _ok_rows_cached(p):
    """依 mtime+size 簽章快取 _parse_xlsx_ok_rows 的結果(行內記憶體快取,跨請求重用)。
    回傳 (rows, names);半截/損毀檔沿用上次的舊快取(若有),否則回 None(呼叫端跳過)。
    回傳的 list 僅供讀取、呼叫端不可改動(快取共用同一物件)。"""
    key = p.name
    sig = _sig(p)
    ent = _OK_ROWS_CACHE.get(key)
    if ent and ent.get("sig") == sig:
        return ent["rows"], ent["names"]
    parsed = _parse_xlsx_ok_rows(p)
    if parsed is None:                       # 半截/損毀:沿用舊快取(若有),否則跳過
        return (ent["rows"], ent["names"]) if ent else None
    _OK_ROWS_CACHE[key] = {"sig": sig, "rows": parsed[0], "names": parsed[1]}
    return parsed


def daily_counts(output_dir=None) -> dict:
    """統計用:讀各機 summary_<機器>_<月>.xlsx(每張工作表一天)+ 尚未併入的 summary_*.csv,
    彙整每日每機的 (queries, bytes)。筆數只計成功檢索(OK / 查無);失敗不算筆數,
    但失敗請求的位元組仍計入流量。Excel 與 CSV 不會重複(併入後 CSV 即刪除)。

    每個月份檔依 mtime+size 命中 _daily_cache.json:過去月份簽章固定 → 不再重新解析整檔,
    只有當月那個檔會因持續寫入而重算。

    回傳 {(date, machine): {"queries": int, "bytes": int}},machine 為機器代號;
    無機器代號的舊 CSV 以空字串表示(呼叫端可顯示為「未標記」)。
    """
    out = Path(output_dir) if output_dir else OUTPUT_DIR
    res = {}
    if not out.exists():
        return res
    _purge_legacy_cache(out)             # 清掉舊版殘留在同步夾的快取(只第一次真的刪到)

    def bump(date, mid, q, b):
        if not (q or b):
            return
        acc = res.setdefault((date, mid), {"queries": 0, "bytes": 0})
        acc["queries"] += q
        acc["bytes"] += b

    # 1) 已彙整的 Excel(每台每月一檔、每天一工作表)。依簽章用快取,避免重讀整份歷史。
    merged_names = set()                 # 已併入 Excel 的 CSV 檔名(用來避免與殘留 CSV 重複)
    if load_workbook is not None:
        cache = _load_cache(out)
        new_cache = {}
        for p, mid, _month in _summary_xlsx_files(out):
            key = p.name
            sig = _sig(p)
            ent = cache.get(key)
            if ent and ent.get("sig") == sig:
                days, names = ent["days"], ent.get("names", [])
            else:
                parsed = _parse_xlsx_counts(p)
                if parsed is None:       # 半截/損毀:沿用舊快取(若有),否則整檔跳過
                    if ent:
                        new_cache[key] = ent
                        days, names = ent["days"], ent.get("names", [])
                    else:
                        continue
                else:
                    days, names = parsed
                    new_cache[key] = {"sig": sig, "days": days, "names": names}
            if key not in new_cache:     # 命中快取的也要保留進新快取
                new_cache[key] = ent
            merged_names.update(names)
            for date, qb in days.items():
                bump(date, mid, qb[0], qb[1])
        if new_cache != cache:           # 內容沒變就不重寫(本機暫存,省去無謂寫入)
            _save_cache(out, new_cache)

    # 2) 尚未併入的 CSV(過渡,或被硬殺殘留的;依表頭對齊,任何舊格式都算得對)。
    #    已登記在某台 _merged 的(代表資料已在 Excel,只是檔還沒刪)就跳過,避免重複。
    for p in out.glob("summary_*.csv"):
        m = CSV_RE.search(p.name)
        if not m or p.name in merged_names:
            continue
        mid = m.group(3) or ""           # 空 = 舊檔未標機器(合法);有值但非法 = 衝突複本
        if mid and _is_conflict_mid(mid):
            continue
        q = b = 0
        try:
            for (idx, patent, status, hits, bb, code) in _read_csv_rows(p):
                if str(status).startswith("OK") or status == "NO_RESULT":
                    q += 1
                b += bb if isinstance(bb, int) else 0
        except Exception:
            continue
        bump(_sheet_for(m.group(1)), mid, q, b)

    return res


def ok_runs(output_dir=None, since: str = None) -> dict:
    """API 額度用:回傳 {(date, mid): [(run_ts, ok_count, code), ...]}。
    ok_count = 該次執行的 OK 列數 ≈ 實際下載到的記錄筆數(『輸出筆數』;NO_RESULT/失敗不算)。
    run_ts = 'YYYY-MM-DD HH:MM:SS'(該次執行開跑時刻),供呼叫端依基準時間過濾。
    code = 該次執行實際用的 userCode(舊資料沒記 → 空字串,呼叫端退回機器對應);
           傳統檢索為 "web"。呼叫端據此把額度歸到「執行當下的碼」,中途切換也分帳正確。

    since 給定時(如時段起點 'YYYY-MM-DD HH:MM:SS'),早於該月份的月份檔整個跳過 —— 額度
    只看「當前時段」,沒必要去翻過去月份。來源同 daily_counts:Excel(每列帶開跑時刻 time 欄)
    + 尚未併入的殘留 CSV(檔名即開跑時刻);因此被『立即停止』硬殺的執行其輸出也算得到。"""
    out = Path(output_dir) if output_dir else OUTPUT_DIR
    res = {}                              # (date, mid) -> {run_ts: [ok_count, code]}
    if not out.exists():
        return {}
    since_month = since[:7] if since else None

    def bump(date, mid, run_ts, ok, code):
        if not ok:
            return
        d = res.setdefault((date, mid), {})
        cur = d.get(run_ts)
        if cur is None:
            d[run_ts] = [ok, code or ""]
        else:
            cur[0] += ok
            if code and not cur[1]:       # 同一次執行碼一致;先到的空、後到有值就補上
                cur[1] = code

    merged_names = set()
    if load_workbook is not None:
        for p, mid, month in _summary_xlsx_files(out):
            if since_month and month and month < since_month:
                continue                  # 早於所需月份的檔:直接跳過(額度只看當前時段)
            got = _ok_rows_cached(p)       # 依簽章命中快取;半截/損毀回 None → 跳過
            if got is None:
                continue
            rows, names = got
            merged_names.update(names)
            for date, run_ts, _patent, code in rows:
                bump(date, mid, run_ts, 1, code)   # 每筆 OK 列計 1(時段輸出筆數)

    for p in out.glob("summary_*.csv"):
        m = CSV_RE.search(p.name)
        if not m or p.name in merged_names:
            continue
        mid = m.group(3) or ""
        if mid and _is_conflict_mid(mid):
            continue
        date = _sheet_for(m.group(1))
        hms = m.group(2)
        run_ts = f"{date} {hms[:2]}:{hms[2:4]}:{hms[4:6]}"
        ok = 0
        code = ""
        try:
            for (idx, patent, status, hits, bb, c) in _read_csv_rows(p):
                if str(status).startswith("OK"):
                    ok += 1
                if c and not code:
                    code = c
        except Exception:
            continue
        bump(date, mid, run_ts, ok, code)

    return {k: [(rt, v[0], v[1]) for rt, v in d.items()] for k, d in res.items()}


def ok_by_case(after_ts, output_dir=None) -> dict:
    """『單筆案號每小時』監看用:只計開跑時刻 run_ts >= after_ts 的 **OK 列**,
    依 (機器, 驗證碼) + 案號統計被輸出幾次。回傳 {(mid, code): {patent: ok_count}}。
    code = 該次執行用的 userCode(舊資料空字串 → 呼叫端退回機器對應;web 為 "web")。
    (同一案號被查越多次、count 越高;對照官方每小時 300/1000 即可示警。)
    after_ts 是當前整點(今天),故早於該月份的月份檔整個跳過,不必翻歷史。"""
    out = Path(output_dir) if output_dir else OUTPUT_DIR
    res = {}                              # (mid, code) -> {patent: count}
    if not out.exists():
        return res
    after_month = (after_ts or "")[:7] or None

    def bump(mid, code, patent, run_ts):
        if not patent or run_ts < after_ts:
            return
        d = res.setdefault((mid, code or ""), {})
        d[patent] = d.get(patent, 0) + 1

    merged_names = set()
    if load_workbook is not None:
        for p, mid, month in _summary_xlsx_files(out):
            if after_month and month and month < after_month:
                continue                  # 早於當前整點所在月份的檔:跳過
            got = _ok_rows_cached(p)       # 與 ok_runs 共用同一份「OK 列」快取(同檔只解析一次)
            if got is None:
                continue
            rows, names = got
            merged_names.update(names)
            for _date, run_ts, patent, code in rows:
                bump(mid, code, patent, run_ts)   # bump 內依 after_ts 過濾(只計當前整點)

    for p in out.glob("summary_*.csv"):
        m = CSV_RE.search(p.name)
        if not m or p.name in merged_names:
            continue
        mid = m.group(3) or ""
        if mid and _is_conflict_mid(mid):
            continue
        hms = m.group(2)
        run_ts = f"{_sheet_for(m.group(1))} {hms[:2]}:{hms[2:4]}:{hms[4:6]}"
        if run_ts < after_ts:
            continue
        try:
            for (idx, patent, status, hits, bb, c) in _read_csv_rows(p):
                if str(status).startswith("OK"):
                    bump(mid, c, str(patent or "").strip(), run_ts)
        except Exception:
            continue
    return res


if __name__ == "__main__":
    # 直接執行 = 先把舊單檔拆成月份檔,再把現有所有 CSV 併進各月份 Excel(可重複執行)。
    moved = migrate_split_monthly()
    if moved:
        print(f"已把 {moved} 個舊單檔拆成月份檔(原檔保留為 .bak)。")
    n = consolidate()
    print(f"已併入 {n} 個 CSV 到各機 summary_<機器>_<月>.xlsx。")
