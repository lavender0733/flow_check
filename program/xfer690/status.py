"""事件 + 系統設定 + 現在時間 → 各系統、各期、各步驟的狀態。純函式。

一期 = KM 上的一列:每日系統是日期,期別系統是公告期數(VOL),不定期系統(英譯、
清單外的作業)是那次執行的開始時刻。系統設定的 reruns(補跑 job)另外放在 rerun_runs。

期別不能靠時間推算:一次手動執行可能連跑好幾期(全文 2415 → 2414 → 2413 相隔 2 分鐘),
也可能補跑很舊的期。所以先把同一系統的信切成「段」(group_runs),每段從帶 VOL 的那封信
取得期別,段裡其他只抓了 metadata 的信沿用它。
"""

import re
from datetime import datetime, time, timedelta

from xfer690.parser import TPE
from xfer690.periods import date_of_vol, vol_twa, vol_twb

GAP = timedelta(hours=2)        # 相隔超過這麼久就是另一次執行
VOL_TRACK = 31                  # TWA / TWB 只追蹤最近一個月
ADHOC_DAYS = 7                  # 清單外的作業只看近 7 天
BAD = ("fail", "unknown", "missing")
_VOL_FN = {"vol_twa": vol_twa, "vol_twb": vol_twb}
_JOB_SUFFIX = re.compile(r"-step\d+-.*$|-auto$")


def _at(ev) -> datetime:
    return datetime.fromisoformat(ev["at"]).astimezone(TPE)


def _is_vol(system) -> bool:
    return str(system.get("period", "")).startswith("vol")


def period_of(event: dict, system: dict, run_period):
    """這封信屬於哪一期。期別系統以信裡的 VOL 為準,沒有就沿用同一段已知的期別。

    每日系統一律是那次執行的日期(run_period;單獨呼叫時用信的日期)。不採用 log 裡的
    DOWNLOAD_DATE:只有失敗的信才會下載 log,若有 log 的信改用資料日期,同一次執行會被
    拆到兩天,還會撞上前一天那次執行。
    """
    period = system.get("period")
    if period == "date":
        return run_period or _at(event).date().isoformat()
    if _is_vol(system):
        v = str((event.get("data") or {}).get("VOL") or "")
        return str(int(v)) if v.isdigit() else run_period
    return run_period


def group_runs(events: list, system: dict) -> list:
    """同一系統的信依時間排序後切段:這封是 steps[0](step1-raw),或跟上一封相隔超過 2 小時,就開新段。"""
    first = (system.get("steps") or [None])[0]
    runs, last = [], None
    for ev in sorted((e for e in events if e.get("at")), key=_at):
        t = _at(ev)
        if not runs or t - last > GAP or (first and ev.get("code") == first):
            runs.append([])
        runs[-1].append(ev)
        last = t
    return runs


def _run_period(seg: list, system: dict):
    start = _at(seg[0])
    if system.get("period") == "date":
        return start.date().isoformat()
    if _is_vol(system):
        for ev in seg:
            v = str((ev.get("data") or {}).get("VOL") or "")
            if v.isdigit():
                return str(int(v))
        return None
    return start.strftime("%Y-%m-%d %H:%M")


def step_pairs(steps: list) -> list:
    """[[步驟, check 或 None], …]。check 的代碼是偶數、緊接在步驟後面(012001 / 012002)。"""
    have = set(steps)
    out = []
    for c in steps:
        n = int(c)
        if n % 2 == 0 and f"{n - 1:06d}" in have:
            continue
        chk = f"{n + 1:06d}"
        out.append([c, chk if n % 2 == 1 and chk in have else None])
    return out


def cell_done(period: dict, step: str, check=None) -> bool:
    """這一格可以打 v 了嗎。步驟本身成功,有 check 的話 check 也要成功、而且不早於步驟的最後一封
    (步驟失敗重跑後,前一次的 check 不算數)。無資料不算完成:每日系統的無資料由
    wiki.plan_fills 另外填 nodata_value。"""
    steps = period.get("steps", {})
    s = steps.get(step) or {}
    if s.get("state") != "ok":
        return False
    if not check:
        return True
    c = steps.get(check) or {}
    return c.get("state") == "ok" and (c.get("at") or "") >= (s.get("at") or "")


def _overall(states: list) -> str:
    for st in ("fail", "missing", "unknown", "pending"):
        if st in states:
            return st
    if states and all(s == "nodata" for s in states):
        return "nodata"
    return "ok"


def _period_date(system: dict, key: str):
    p = system.get("period")
    if p == "date":
        return key
    if _is_vol(system):
        # period 是 "vol" 的全文不從排程推算,TWA / TWB 由 view 決定
        kind = {"vol_twa": "twa", "vol_twb": "twb"}.get(p, system.get("view"))
        try:
            return date_of_vol(kind, int(key)).isoformat()
        except (KeyError, ValueError):
            return None
    return key[:10]


def _deadline(system: dict, day: str):
    h, m = (int(x) for x in str(system.get("expect_by") or "23:59").split(":"))
    return datetime.combine(datetime.fromisoformat(day).date(), time(h, m), tzinfo=TPE)


def _empty_step(state):
    return {"state": state, "at": None, "runs": 0, "event": None}


def _build_period(system: dict, key: str, evs: list, now: datetime) -> dict:
    by_code = {}
    for ev in sorted(evs, key=_at):
        by_code.setdefault(ev["code"], []).append(ev)
    steps = {c: {"state": lst[-1].get("status") or "unknown", "at": lst[-1]["at"],
                 "runs": len(lst), "event": lst[-1]} for c, lst in by_code.items()}
    auto = {c: steps.pop(c) for c in list(steps) if c in system.get("auto", [])}
    # 只看每個步驟最後一封:09-29 無資料、09-30 補上重跑成功的那一期,以重跑為準。
    nodata = any(v["state"] == "nodata" for v in steps.values())
    last_at = max(_at(ev) for ev in evs)

    if nodata:
        for c in system.get("steps", []):
            steps[c] = dict(steps.get(c) or _empty_step(None), state="nodata")
    elif system.get("configured"):
        failed = any(v["state"] == "fail" for v in steps.values())
        if system.get("period") == "date":
            due = _deadline(system, key)
        else:
            due = last_at + GAP
        for c in system.get("steps", []):
            if c not in steps:
                steps[c] = _empty_step("none" if failed else ("pending" if now < due else "missing"))

    states = [v["state"] for v in steps.values()]
    if not nodata and any(a["state"] == "fail" for a in auto.values()):
        states.append("fail")
    state = _overall(states)
    if nodata and _is_vol(system):
        state = "fail"      # 期別系統的無資料 = 那一期的檔案還沒到,要處理

    tag, sud_types = None, None
    for ev in sorted(evs, key=_at):
        tag = (ev.get("data") or {}).get("TAG") or tag
        sud_types = ev.get("sud_types") or sud_types
    return {"key": key, "date": _period_date(system, key), "state": state, "nodata": nodata,
            "steps": steps, "auto": auto, "tag": tag, "sud_types": sud_types,
            "first_at": min(_at(ev) for ev in evs).isoformat(), "last_at": last_at.isoformat()}


def _expected(system: dict, today, days: int, first_at) -> list:
    """[(期別鍵, 日期)]:該有信的期。期限早於信箱裡第一封信的,當時通知還沒開,不預期。"""
    sched = system.get("schedule")
    out = []
    if sched == "daily" and system.get("period") == "date":
        for i in range(days):
            d = today - timedelta(days=i)
            out.append((d.isoformat(), d))
    elif isinstance(sched, dict) and system.get("period") in _VOL_FN:
        mdays = sorted(sched.get("monthdays") or [])
        d = today
        while len(out) < len(mdays) and (today - d).days < 62:
            if d.day in mdays:
                out.append((str(_VOL_FN[system["period"]](d)), d))
            d -= timedelta(days=1)
    return [(k, d) for k, d in out
            if first_at is None or _deadline(system, d.isoformat()) >= first_at]


def job_of(ev: dict) -> str:
    """信屬於哪個作業:名稱去掉 -stepN-… / -auto(legal-backfill-step2-l1 → legal-backfill),沒名稱用代碼。"""
    return _JOB_SUFFIX.sub("", ev.get("name") or "") or ev["code"]


def _latest_per_job(system: dict, evs: list, now: datetime) -> dict:
    """清單外的作業與補跑:同名的作業不論成功失敗,只留最新一次。

    先依作業名稱分開再切段,連著跑的兩個作業(相隔不到 2 小時)才不會併成一次。"""
    by_job = {}
    for ev in evs:
        by_job.setdefault(job_of(ev), []).append(ev)
    out = {}
    for job, lst in by_job.items():
        seg = group_runs(lst, {"steps": []})[-1]
        out[job] = dict(_build_period(system, _run_period(seg, system), seg, now), job=job)
    return out


def _system_status(system: dict, evs: list, now: datetime, days: int, first_at, reruns=()) -> dict:
    today = now.date()
    kind = system.get("kind")
    if kind == "adhoc":
        periods = _latest_per_job(system, evs, now)
    else:
        grouping = system if system.get("configured") else {"steps": []}
        buckets = {}
        for seg in group_runs(evs, grouping):
            rp = _run_period(seg, system)
            for ev in seg:
                key = period_of(ev, system, rp)
                if key is not None:
                    buckets.setdefault(key, []).append(ev)
        periods = {k: _build_period(system, k, v, now) for k, v in buckets.items()}

    if kind == "system":
        if system.get("period") == "date":
            cutoff = today - timedelta(days=days - 1)
        else:
            cutoff = today - timedelta(days=VOL_TRACK - 1)
    else:
        cutoff = today - timedelta(days=ADHOC_DAYS - 1)
    periods = {k: p for k, p in periods.items()
               if datetime.fromisoformat(p["last_at"]).date() >= cutoff}

    if kind == "system":
        for key, d in _expected(system, today, days, first_at):
            if key in periods:
                periods[key]["expected"] = True
                continue
            st = "pending" if now < _deadline(system, d.isoformat()) else "missing"
            periods[key] = {"key": key, "date": d.isoformat(), "state": st, "nodata": False,
                            "steps": {c: _empty_step(st) for c in system.get("steps", [])},
                            "auto": {}, "tag": None, "sud_types": None, "first_at": None, "last_at": None,
                            "expected": True}

    ordered = sorted(periods.values(), key=lambda p: (p["date"] or "", p["key"]), reverse=True)
    if kind == "maint":
        ordered = ordered[:1]
    if kind == "system":
        alerts = sum(1 for p in ordered if p["state"] in BAD)
    elif kind == "adhoc":
        alerts = sum(1 for p in ordered if p["state"] == "fail")
    else:
        alerts = 0

    names = {}
    for ev in sorted(evs, key=_at):
        if ev.get("name"):
            names[ev["code"]] = ev["name"]
    groups = sorted({ev.get("group") for ev in evs if ev.get("group")})
    keep = ("id", "label", "schedule", "period", "expect_by", "configured", "kind",
            "view", "type", "auto", "steps", "reruns")
    out = {k: system.get(k) for k in keep}
    out.update(pairs=step_pairs(system.get("steps", [])), names=names, groups=groups,
               periods=ordered, alerts=alerts)
    if kind == "system" and system.get("reruns"):
        # 這個系統的補跑 job(設定的 reruns):跟清單外的作業一樣,近 7 天、同名只留最新一次
        sub = _system_status({"id": system["id"], "kind": "adhoc", "configured": False, "auto": [],
                              "steps": sorted(system["reruns"]), "period": "none"},
                             list(reruns), now, days, first_at)
        names.update(sub["names"])
        out.update(rerun_runs=sub["periods"], alerts=alerts + sub["alerts"])
    return out


def _unlisted_system(code: str) -> dict:
    """清單外的 job。維運 99xxxx 每個 job 自成一個;其他依代碼前 4 碼歸成一個作業。"""
    if code.startswith("99"):
        sid, kind = code, "maint"
    else:
        sid, kind = code[:4] + "xx", "adhoc"
    return {"id": sid, "label": sid, "kind": kind, "configured": False, "auto": [],
            "steps": [], "schedule": "manual", "period": "none", "expect_by": None}


def build_status(events: list, systems: list, now: datetime, days: int) -> dict:
    now = now.astimezone(TPE)
    configured = [dict(s, configured=True, kind="system") for s in systems]
    owner, rerun_of = {}, {}
    for s in configured:
        for c in list(s.get("auto", [])) + list(s.get("steps", [])):
            owner[c] = s
        for c in s.get("reruns", []):
            rerun_of[c] = s["id"]
    extra, buckets, reruns = {}, {}, {}
    known = [ev for ev in events if ev.get("code") and ev.get("at")]
    for ev in known:
        if ev["code"] in rerun_of:
            reruns.setdefault(rerun_of[ev["code"]], []).append(ev)
            continue
        s = owner.get(ev["code"])
        if s is None:
            u = _unlisted_system(ev["code"])
            s = extra.setdefault(u["id"], u)
        buckets.setdefault(s["id"], []).append(ev)
    for s in extra.values():
        s["steps"] = sorted({ev["code"] for ev in buckets[s["id"]]})

    first_at = min((_at(ev) for ev in known), default=None)
    out = [_system_status(s, buckets.get(s["id"], []), now, days, first_at, reruns.get(s["id"], []))
           for s in configured + sorted(extra.values(), key=lambda x: x["id"])]
    return {"date": now.date().isoformat(), "systems": out,
            "alerts": sum(s["alerts"] for s in out)}
