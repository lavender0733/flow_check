"""Wiki 登記:依狀態規劃要填的格(plan_fills)。"""

from datetime import date

from xfer690.status import cell_done


def _new_row(template: dict, d, vol: str) -> dict:
    d = date.fromisoformat(d) if d else None
    vals = {"vol": vol or ""}
    if d:
        vals.update(yyyy=f"{d.year:04d}", mm=f"{d.month:02d}", dd=f"{d.day:02d}",
                    yyyymmdd=d.strftime("%Y%m%d"))
    return {int(c): t.format(**vals) for c, t in (template or {}).items()}


def _row_key(target: dict, p: dict):
    if target.get("key") == "vol":
        return p["key"]
    if not p.get("date"):
        return None
    return date.fromisoformat(p["date"]).strftime(target.get("key_format") or "%Y-%m-%d")


def plan_fills(status: dict, target: dict, wiki_cfg: dict) -> dict:
    """{列鍵: {"cells": {欄號: 值}, "new": {欄號: 值}}}。沒有要填的格的期不列入。"""
    sys = next((s for s in status.get("systems", []) if s["id"] == target.get("system")), None)
    if sys is None:
        return {}
    out = {}
    for p in sys.get("periods", []):
        if not p.get("last_at"):
            continue        # 一封信都沒有的期(該到沒到 / 還沒跑)
        key = _row_key(target, p)
        if key is None:
            continue
        cells = {}
        for col, c in (target.get("cols") or {}).items():
            kind = c.get("value")
            if kind == "v":
                if cell_done(p, c["step"], c.get("check")):
                    cells[int(col)] = "v"
            elif kind == "tag":
                if p.get("tag"):
                    cells[int(col)] = str(p["tag"])
            elif kind == "writes":
                st = (p.get("steps") or {}).get(c.get("step")) or {}
                w = (st.get("event") or {}).get("writes")
                if w is not None and p.get("state") == "ok":
                    cells[int(col)] = str(w)
        if cells:
            out[key] = {"cells": cells, "new": _new_row(target.get("new_row"), p.get("date"), p["key"])}
    return out
