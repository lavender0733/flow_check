"""Wiki 登記:規劃要填的格(plan_fills)、Wiki.js GraphQL 用戶端、整頁同步與預覽。

寫入只在使用者按「寫入 Wiki」時發生;預覽用同一套流程、只是不送更新。
"""

import json
import re
import urllib.error
import urllib.request
from datetime import date

from xfer690.status import cell_done
from xfer690.wikitable import (LabelMismatch, TableNotFound, apply_edits, apply_fills, locate,
                               read_rows)


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
    sys_ = next((s for s in status.get("systems", []) if s["id"] == target.get("system")), None)
    if sys_ is None:
        return {}
    out = {}
    for p in sys_.get("periods", []):
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


# ============================================================
# Wiki.js 2.x GraphQL
# ============================================================
class WikiError(Exception):
    pass


class WikiAuthError(WikiError):
    """憑證過期、被拒或權限不足:要重新登入。"""


_AUTH_RE = re.compile(
    r"forbidden|unauthori[sz]ed|unauthenticated|not authenticated|invalid token|jwt", re.I)

_PAGE_FIELDS = ("id content title description editor isPublished isPrivate locale path "
                "tags { tag } scriptCss scriptJs")

_LOGIN_Q = ("mutation($u:String!,$p:String!,$s:String!){authentication{login(username:$u,"
            "password:$p,strategy:$s){responseResult{succeeded errorCode message} jwt}}}")
_PAGE_Q = ("query($p:String!,$l:String!){pages{singleByPath(path:$p,locale:$l){"
           + _PAGE_FIELDS + "}}}")
_UPDATE_Q = ("mutation($id:Int!,$content:String!,$description:String!,$editor:String!,"
             "$isPrivate:Boolean!,$isPublished:Boolean!,$locale:String!,$path:String!,"
             "$scriptCss:String,$scriptJs:String,$tags:[String]!,$title:String!){pages{update("
             "id:$id,content:$content,description:$description,editor:$editor,isPrivate:$isPrivate,"
             "isPublished:$isPublished,locale:$locale,path:$path,scriptCss:$scriptCss,"
             "scriptJs:$scriptJs,tags:$tags,title:$title){responseResult{succeeded errorCode message}}}}")


def _urllib_post(url: str, headers: dict, body: bytes) -> bytes:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise WikiAuthError(f"Wiki 回 HTTP {e.code}") from e
        raise WikiError(f"Wiki 回 HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise WikiError(f"連不上 Wiki:{e}") from e


class WikiClient:
    def __init__(self, url: str, token: str = None, post=None):
        self.url = url.rstrip("/") + "/graphql"
        self.token = token
        self._post = post or _urllib_post

    def _gql(self, query: str, variables: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        raw = self._post(self.url, headers,
                         json.dumps({"query": query, "variables": variables}).encode("utf-8"))
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise WikiError("Wiki 回應不是 JSON") from e
        errs = data.get("errors") or []
        if errs:
            msg = "; ".join(str(e.get("message", "")) for e in errs)[:300]
            codes = " ".join(str((e.get("extensions") or {}).get("code", "")) for e in errs)
            if _AUTH_RE.search(msg) or _AUTH_RE.search(codes):
                raise WikiAuthError(msg)
            raise WikiError(msg)
        return data.get("data") or {}

    def login(self, username: str, password: str, strategy: str = "local") -> str:
        r = self._gql(_LOGIN_Q, {"u": username, "p": password, "s": strategy})
        res = (r.get("authentication") or {}).get("login") or {}
        rr = res.get("responseResult") or {}
        if not rr.get("succeeded") or not res.get("jwt"):
            raise WikiAuthError(rr.get("message") or "登入失敗")
        self.token = res["jwt"]
        return self.token

    def get_page(self, path: str, locale: str) -> dict:
        page = (self._gql(_PAGE_Q, {"p": path, "l": locale}).get("pages") or {}).get("singleByPath")
        if not page:
            raise WikiError(f"找不到頁面 {path}")
        return page

    def update_content(self, page: dict, content: str) -> None:
        """除了 content,其他欄位照讀回來的值原樣傳回(tags 轉成字串陣列)。"""
        v = {k: page.get(k) for k in ("id", "description", "editor", "isPrivate", "isPublished",
                                      "locale", "path", "scriptCss", "scriptJs", "title")}
        v["description"] = v["description"] or ""
        v["content"] = content
        v["tags"] = [t["tag"] if isinstance(t, dict) else str(t) for t in (page.get("tags") or [])]
        r = self._gql(_UPDATE_Q, v)
        rr = ((r.get("pages") or {}).get("update") or {}).get("responseResult") or {}
        if not rr.get("succeeded"):
            msg = rr.get("message") or "更新失敗"
            if _AUTH_RE.search(msg):
                raise WikiAuthError(msg)
            raise WikiError(msg)


# ============================================================
# 整頁處理:使用者的編輯 → 自動填格,同時產生預覽
# ============================================================
def page_path(wiki_cfg: dict, page: str) -> str:
    return f"{wiki_cfg['base'].rstrip('/')}/{page}"


def table_title(target: dict) -> str:
    """「TWA全文Full」= 表代碼 + 頁名去掉開頭的 TW;每日表沒有表代碼就用頁名。"""
    code = target.get("table") or ""
    page = target["page"]
    return code + (page[2:] if code and page.startswith("TW") else page)


def _col_index(labels: list, targets: list, col, system=None):
    """欄名 → 欄號。同名的欄(每日表有 4 個 RAW)用 system 指定是哪個系統的。"""
    if isinstance(col, int) or str(col).isdigit():
        return int(col)
    for t in targets:
        if system and t["system"] != system:
            continue
        for i, c in (t.get("cols") or {}).items():
            if c.get("label") == col:
                return int(i)
    hits = [i for i, lab in enumerate(labels) if lab == col]
    return hits[0] if len(hits) == 1 else None


def _period_map(status: dict, target: dict) -> dict:
    sys_ = next((s for s in status.get("systems", []) if s["id"] == target["system"]), None)
    out = {}
    for p in (sys_ or {}).get("periods", []):
        if p.get("last_at"):
            k = _row_key(target, p)
            if k:
                out[k] = p
    return out


def _new_report():
    return {"filled": 0, "inserted": 0, "edited": 0, "conflicts": [], "errors": [], "skipped": []}


def process_page(content: str, targets: list, status: dict, wiki_cfg: dict, edits=()) -> tuple:
    """一頁的所有 target:先套使用者的編輯,再自動填空格。

    回傳 (新內容, report, tables)。tables 是預覽用的表格(同一張表的多個 target 合併)。
    每列每格的 state:fill(這次要填)、same / old(頁面上原本就有)、conflict(原本有、
    但跟 log 不同)、hold(失敗或沒到,留空)、wait(還沒跑到)。
    """
    ph = wiki_cfg.get("placeholder_re") or ""
    report = _new_report()
    groups = {}
    for t in targets:
        groups.setdefault(t.get("table") or "", []).append(t)
    tables = []
    for code, ts in groups.items():
        title = table_title(ts[0])
        tb = {"page": ts[0]["page"], "table": code, "title": title, "error": None,
              "key_col": int(ts[0].get("key_col", 0)), "columns": [], "rows": {}, "skipped": []}
        tables.append(tb)
        try:
            labels = locate(content.splitlines(keepends=True), ts[0])["labels"]
        except TableNotFound as e:
            tb["error"] = str(e)
            report["errors"].append(f"{title}:{e}")
            continue

        # 使用者在畫面上明確改過的格(唯一會覆寫非空值的路徑)
        edited = set()
        mine = [e for e in edits if (e.get("table") or "") == code]
        if mine:
            resolved = []
            for e in mine:
                ci = _col_index(labels, ts, e.get("col"), e.get("system"))
                if ci is None:
                    report["conflicts"].append({"table": title, "key": e.get("key"), "col": e.get("col"),
                                                "existing": None, "wanted": e.get("value"),
                                                "reason": "找不到這一欄"})
                    continue
                resolved.append(dict(e, col=ci))
            try:
                content, er = apply_edits(content, ts[0], resolved)
            except TableNotFound as e:
                report["errors"].append(f"{title}:{e}")
                continue
            report["edited"] += er["edited"]
            for c in er["conflicts"]:
                report["conflicts"].append(dict(c, table=title, reason="頁面上的值已經被改過"))
            edited = {(str(e["key"]), e["col"]) for e in resolved}

        # 顯示的欄:列鍵與新列範本的欄(資訊),加上程式負責的欄
        info = {int(ts[0].get("key_col", 0))}
        for t in ts:
            info |= {int(c) for c in (t.get("new_row") or {})}
        cols = {}
        for i in info:
            cols[i] = {"col": i, "label": labels[i] if i < len(labels) else "", "system": None,
                       "kind": "key" if i == tb["key_col"] else "info"}
        for t in ts:
            group = next((s.get("label") for s in status.get("systems", []) if s["id"] == t["system"]),
                         t["system"])
            for i, c in (t.get("cols") or {}).items():
                cols[int(i)] = {"col": int(i), "label": c.get("label"), "system": t["system"],
                                "group": group, "kind": c.get("value"), "step": c.get("step")}
        tb["columns"] = [cols[i] for i in sorted(cols)]

        for t in ts:
            plan = plan_fills(status, t, wiki_cfg)
            try:
                content, rep = apply_fills(content, t, plan, ph)
            except (TableNotFound, LabelMismatch) as e:
                tb["error"] = str(e)
                report["errors"].append(f"{title}:{e}")
                continue
            report["filled"] += rep["filled"]
            report["inserted"] += rep["inserted"]
            for k in rep["skipped"]:
                if k not in tb["skipped"]:
                    tb["skipped"].append(k)
                    report["skipped"].append(f"{title} {k}")
            for c in rep["conflicts"]:
                if (str(c["key"]), int(c["col"])) not in edited:
                    report["conflicts"].append(dict(c, table=title, reason="頁面上已經有不同的值"))
            for r in rep["rows"]:
                row = tb["rows"].setdefault(r["key"], {"key": r["key"], "new": r["new"], "cells": {}})
                row["new"] = row["new"] or r["new"]
                row["cells"].update({int(c): v for c, v in r["cells"].items()})

            # 有收到信、但沒排進計畫的格:失敗或沒到 → 留空;還沒跑到 → 待跑
            current = read_rows(content, t)["rows"]
            for key, p in _period_map(status, t).items():
                if key in tb["skipped"] or key not in current:
                    continue
                row = tb["rows"].setdefault(key, {"key": key, "new": False, "cells": {}})
                for i, c in (t.get("cols") or {}).items():
                    i = int(i)
                    if i in row["cells"]:
                        continue
                    have = current[key][i] if i < len(current[key]) else ""
                    if have and not (ph and re.search(ph, have)):
                        row["cells"][i] = {"value": have, "existing": have, "state": "old"}
                        continue
                    pending = p.get("state") == "pending" or any(
                        (p.get("steps", {}).get(x) or {}).get("state") == "pending"
                        for x in (c.get("step"), c.get("check")) if x)
                    row["cells"][i] = {"value": "", "existing": have,
                                       "state": "wait" if pending else "hold"}

        # 每列帶上頁面上(套用後)的整列值,畫面才能顯示列鍵、日期等資訊欄
        final = read_rows(content, ts[0])["rows"]
        rows = []
        for key, row in tb["rows"].items():
            row["row"] = final.get(key, [])
            rows.append(row)
        rows.sort(key=lambda r: (int(r["key"]) if r["key"].isdigit() else 0, r["key"]), reverse=True)
        tb["rows"] = rows
    return content, report, tables


def sync(client, status: dict, wiki_cfg: dict, edits=(), write: bool = True) -> dict:
    """依頁分組,每頁讀一次、處理完內容有變才更新(dry_run 或 write=False 時不更新)。
    一頁失敗不影響其他頁;WikiAuthError 往外拋,讓呼叫端清掉登入狀態。

    回傳 {"pages": {頁名: report + updated/id}, "tables": [...], "contents": {頁名: 目前內容}}。
    """
    pages = {}
    for t in wiki_cfg.get("tables", []):
        pages.setdefault(t["page"], []).append(t)
    out = {"pages": {}, "tables": [], "contents": {}}
    for page, targets in pages.items():
        rep = dict(_new_report(), updated=False, id=None)
        out["pages"][page] = rep
        mine = [e for e in edits if e.get("page") == page]
        try:
            p = client.get_page(page_path(wiki_cfg, page), wiki_cfg.get("locale", "zh-tw"))
            rep["id"] = p.get("id")
            old = p.get("content") or ""
            new, r, tables = process_page(old, targets, status, wiki_cfg, mine)
            rep.update(r)
            out["contents"][page] = old
            if new != old and write and not wiki_cfg.get("dry_run", True):
                client.update_content(p, new)
                rep["updated"] = True
                out["contents"][page] = new
            # 預覽:寫入後看新頁面;沒寫時不含使用者的編輯(那些只存在前端)
            if rep["updated"]:
                _, _, tables = process_page(new, targets, status, wiki_cfg)
            elif mine:
                _, _, tables = process_page(old, targets, status, wiki_cfg)
            out["tables"] += tables
        except WikiAuthError:
            raise
        except Exception as e:      # 一頁失敗不影響其他頁
            rep["errors"].append(str(e)[:300])
    return out


def preview(contents: dict, status: dict, wiki_cfg: dict) -> dict:
    """用上次讀到的頁面內容算預覽(不連網)。沒讀過的頁標 read=False。"""
    pages = {}
    for t in wiki_cfg.get("tables", []):
        pages.setdefault(t["page"], []).append(t)
    out = {"pages": {}, "tables": []}
    for page, targets in pages.items():
        content = (contents or {}).get(page)
        if content is None:
            out["pages"][page] = {"read": False}
            continue
        new, rep, tables = process_page(content, targets, status, wiki_cfg)
        rep.update(read=True, changed=new != content)
        out["pages"][page] = rep
        out["tables"] += tables
    return out
