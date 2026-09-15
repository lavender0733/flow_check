"""
API 額度 / 驗證碼歸戶
====================
從 traffic_monitor.py 抽出。算各 userCode 在當前時段的『時段輸出總筆數』用量、單筆案號
每小時輸出監看,並把每次執行的輸出依『執行當下實際用的碼』歸戶(切換驗證碼也分帳正確)。

對外主要用 api_quota_status() 與 api_case_hourly();其餘為內部輔助。相依:paths(路徑)、
storage(output_store 統計來源 / machine_label / MID_RE),維持單向相依、不回頭匯入主檔。
"""

import re
from datetime import datetime, timedelta

from common.ident import mask_code as _mask_code
from common.jsonio import read_json
from core.paths import BASE, API_QUOTA_BASELINE_PATH
from core.storage import output_store, machine_label, MID_RE


def _load_quota_baseline() -> dict:
    """讀 data/config/api_quota_baseline.json:使用者趁沒跑 API 時,從 TIPO 後台查到的
    各 userCode『當下已用下載筆數』基準。回傳 {小寫userCode: {"used": int, "since": ts}}。
    程式會在此基準上,累加 since 之後 run_log 的用量 → 達到「定時校正」。"""
    data = read_json(API_QUOTA_BASELINE_PATH, default={})
    codes = data.get("codes", {}) if isinstance(data, dict) else {}
    out = {}
    for k, v in codes.items():
        if isinstance(v, dict):
            out[str(k).strip().lower()] = v
    return out


def _current_segment(cfg: dict) -> tuple:
    """當前時段:(busy, seg_start 'YYYY-MM-DD HH:MM:SS', dl_cap, case_cap)。
    官方:上班=週一~五 08:00-18:00,其餘為下班。時段輸出總筆數於 08:00/18:00 重置。"""
    now = datetime.now()
    bs = int(cfg.get("busy_start_hour", 8))
    be = int(cfg.get("busy_end_hour", 18))
    busy = now.weekday() < 5 and bs <= now.hour < be
    if busy:
        seg_start = now.replace(hour=bs, minute=0, second=0, microsecond=0)
        return True, seg_start.strftime("%Y-%m-%d %H:%M:%S"), \
            int(cfg.get("dl_cap_busy", 10000)), int(cfg.get("case_cap_busy", 300))
    # 下班時段起點 = 最近一次 18:00(今天 18:00 後算今天;否則昨天 18:00)
    if now.hour >= be:
        seg_start = now.replace(hour=be, minute=0, second=0, microsecond=0)
    else:
        seg_start = (now - timedelta(days=1)).replace(hour=be, minute=0, second=0, microsecond=0)
    return False, seg_start.strftime("%Y-%m-%d %H:%M:%S"), \
        int(cfg.get("dl_cap_offpeak", 30000)), int(cfg.get("case_cap_offpeak", 1000))


def _code_of_machine(cfg: dict) -> dict:
    """各機 → 目前選用的 userCode(讀 api_config_<機器>.json;衝突複本跳過)。
    僅供『舊資料沒記碼』時的退路:額度歸戶優先採用 output 記錄的執行當下碼。"""
    gpss_dir = BASE / cfg["gpss_dir"]
    out = {}
    for p in gpss_dir.glob("api_config_*.json"):
        mm = re.match(r"api_config_(.+)\.json$", p.name)
        if not mm or not MID_RE.match(mm.group(1)):
            continue
        one = read_json(p, default={})
        out[mm.group(1)] = one.get("userCode", "") if isinstance(one, dict) else ""
    return out


def _effective_code(code: str, mid: str, code_of: dict):
    """這次執行實際歸戶的 userCode。
    - output 有記碼(新資料)→ 就用那個碼(執行當下寫死的,切換驗證碼也分帳正確)。
    - code == 'web' → 傳統檢索,不耗 API 額度,回 None(呼叫端略過)。
    - 沒記碼(舊資料,空字串/None)→ 退回『機器目前選的碼』(舊行為)。"""
    c = (code or "").strip()
    if c.lower() == "web":
        return None
    if c:
        return c
    return code_of.get(mid, "")


def api_quota_status(cfg: dict) -> dict:
    """各 userCode 在「當前時段」的『時段輸出總筆數』用量(官方:上班10000/下班30000,
    於 08:00、18:00 重置)。用量 = 當前時段內本工具輸出(summary 的 OK 列數 ≈ 實際下載記錄;
    每批 flush 故被硬殺的執行也算得到;傳統檢索 web 不耗額度、不在內)。
    可用 api_quota_baseline.json 校正,但基準只在『落在當前時段內』才採用(跨時段已重置)。"""
    busy, seg_start, cap, _case_cap = _current_segment(cfg)
    baseline = _load_quota_baseline()
    code_of = _code_of_machine(cfg)

    # 收「當前時段內(run_ts >= seg_start)」各次執行的輸出筆數(OK 列數),依『執行當下
    # 實際用的 userCode』歸戶 —— 而非事後查機器目前選哪組,故中途切換驗證碼也分帳正確。
    groups = {}                           # userCode -> {userCode, machines:set, entries:[(run_ts,ok)]}
    out_dir = BASE / cfg["gpss_dir"] / "output"
    if output_store is not None:
        try:
            for (_d, mid), runs in output_store.ok_runs(out_dir, since=seg_start).items():
                for run_ts, ok, code in runs:
                    if run_ts < seg_start:
                        continue
                    uc = _effective_code(code, mid, code_of)
                    if uc is None:        # 傳統檢索 web:不耗 API 額度,不計入
                        continue
                    g = groups.setdefault(uc or f"?{mid}",
                                          {"userCode": uc, "machines": set(), "entries": []})
                    g["machines"].add(machine_label(mid))
                    g["entries"].append((run_ts, ok))
        except Exception:
            pass

    def make_row(user_code, machines, entries):
        b = baseline.get((user_code or "").lower())
        since = (b or {}).get("since") or ""
        # 基準只在落在當前時段內才採用(時段邊界已重置,跨時段的基準無效)。
        if since and since >= seg_start:
            floor, base_used = since, int((b or {}).get("used", 0) or 0)
        else:
            floor, base_used = seg_start, 0
        seg_out = sum(ok for run_ts, ok in entries if run_ts > floor)
        used = base_used + seg_out
        return {
            "code": _mask_code(user_code),
            "machines": sorted(machines),
            "seg_output": seg_out,
            "baseline": base_used,
            "since": since if base_used else "",
            "est_used": used,
            "remaining": max(0, cap - used),
            "pct": round(used / cap * 100) if cap else 0,
        }

    codes = [make_row(g["userCode"], g["machines"], g["entries"]) for g in groups.values()]
    shown = {(g["userCode"] or "").lower() for g in groups.values()}
    for uc_low in baseline:                # 有基準但本時段沒活動的也顯示
        if uc_low in shown:
            continue
        orig = next((c for c in code_of.values() if c.lower() == uc_low), uc_low)
        codes.append(make_row(orig, set(), []))

    codes.sort(key=lambda c: c["pct"], reverse=True)
    return {"segment": "busy" if busy else "offpeak", "cap": cap,
            "seg_start": seg_start, "codes": codes}


def api_case_hourly(cfg: dict) -> dict:
    """「單筆案號每小時輸出」監看(官方:上班 300、下班 1000,每整點重置)。
    回傳當前整點內、各 userCode 被輸出最多次的前幾個案號,接近上限就示警。
    這是使用者實際會撞的那條限制(同一案號查太多次)。"""
    now = datetime.now()
    hour_start = now.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    busy, _seg, _cap, case_cap = _current_segment(cfg)
    code_of = _code_of_machine(cfg)

    groups = {}                           # userCode(原碼) -> {patent: count}
    out_dir = BASE / cfg["gpss_dir"] / "output"
    if output_store is not None:
        try:
            for (mid, code), cases in output_store.ok_by_case(hour_start, out_dir).items():
                uc = _effective_code(code, mid, code_of)
                if uc is None:            # 傳統檢索 web:不耗 API 額度,不計入
                    continue
                uc = uc or f"?{mid}"
                g = groups.setdefault(uc, {})
                for pat, c in cases.items():
                    g[pat] = g.get(pat, 0) + c
        except Exception:
            pass

    codes = []
    for uc, cases in groups.items():
        top = sorted(cases.items(), key=lambda kv: kv[1], reverse=True)[:5]
        mx = top[0][1] if top else 0
        codes.append({
            "code": _mask_code(uc if not uc.startswith("?") else ""),
            "max": mx,
            "near": mx >= case_cap * 0.8,
            "top": [{"patent": p, "count": c} for p, c in top if c > 1],
        })
    codes.sort(key=lambda c: c["max"], reverse=True)
    return {"hour": now.strftime("%H:00"), "case_cap": case_cap, "codes": codes}
