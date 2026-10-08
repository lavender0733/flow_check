"""s690 整合式專利檢索(cloud.tipo.gov.tw/s690/ui/)的查詢:KM 書目表的 690查詢筆數與 SUD 抽測、
TW影像GCS 的說明書 / 代表圖。

網頁背後是 oRPC:POST <BASE>/<路徑>,body {"json": {...}},回 {"json": {...}}。不用登入
(2026-10-07 實測)。沒有公開文件,對方改版就可能壞:任何錯誤都丟 S690Error,呼叫端記下、不填格。
"""

import json
import re
import urllib.error
import urllib.request

BASE = "https://cloud.tipo.gov.tw/s690/ui/main/rpc/patent"
USER_AGENT = "flow-check-xfer690/1"
PAGE = 40
MAX_PAGES = 3
CATALOG = {"TWB": "sud00_1", "TWA": "pubsud01"}     # 公告目錄 / 發明公開目錄:每案都有,不算異動
_REP_RE = re.compile(r"<representative[\s>]")
_SPACE_RE = re.compile(r"[\s　]+")
_SPLIT_RE = re.compile(r"[、;;,,]")


class S690Error(Exception):
    pass


def _urllib_post(url: str, body: bytes) -> bytes:
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise S690Error(f"s690 回 HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise S690Error(f"連不上 s690:{e}") from e


def split_vol(vol) -> tuple:
    """公報期數 → (卷號, 期數):2413 → ("24", "13")。期數不補零(s690 存的是數字)。"""
    v = str(int(vol))
    return v[:-2], str(int(v[-2:]))


def _options(case_type: str) -> dict:
    """進階檢索「展開設定 → 案件類型」:TWA 公開、TWB 公告。"""
    return {"countryCodes": ["TWA", "TWB"], "onlyDomestic": True, "caseTypeCodes": [case_type],
            "caseStatusCodes": [], "patentTypeCodes": [], "rightsChangeTypeCodes": [],
            "dualApplication": False}


def _norm(s) -> str:
    return _SPACE_RE.sub("", str(s or ""))


def _items(rec: dict) -> dict:
    return {x.get("title"): x.get("content") for x in rec.get("items") or []}


def _same_issue(rec: dict, vol: str, isu: str) -> bool:
    try:
        return int(rec.get("volno")) == int(vol) and int(rec.get("isuno")) == int(isu)
    except (TypeError, ValueError):
        return False


def _pn_of(case: dict) -> str:
    if not case.get("pnRaw"):
        raise S690Error("s690 回傳的案件沒有公開 / 公告號")
    return "TW" + str(case["pnRaw"])


def _judge(case: dict, rec: dict) -> dict:
    """抽到的案件 + 那筆雜項 → 判定。讓與看目前專利權人、消滅看案件狀態,其他類型只確認查得到。"""
    t = rec.get("type")
    name = rec.get("typeName") or t
    out = {"pn": _pn_of(case), "type": t, "type_name": name}
    if t == "sud11":
        who = _items(rec).get("受讓人") or ""
        now = case.get("currentAssignees") or []
        have = {_norm(x) for x in now}
        parts = [p for p in _SPLIT_RE.split(str(who)) if _norm(p)]
        ok = bool(parts) and all(_norm(p) in have for p in parts)
        out.update(ok=ok, detail=f"受讓人 {who or '(空白)'};書目目前專利權人 {'、'.join(now) or '(空白)'}")
    elif t == "sud08":
        ok = case.get("caseStatusCode") == "expired"
        out.update(ok=ok, detail=f"專利權消滅;書目案件狀態 {case.get('caseStatus') or case.get('caseStatusCode')}")
    else:
        out.update(ok=True, detail=f"{name} 刊在 {rec.get('volno')} 卷 {rec.get('isuno')} 期")
    return out


class S690Client:
    def __init__(self, post=None):
        self._post = post or _urllib_post

    def _rpc(self, path: str, body: dict) -> dict:
        raw = self._post(f"{BASE}/{path}", json.dumps({"json": body}).encode("utf-8"))
        try:
            data = json.loads(raw)["json"]
        except (ValueError, KeyError, TypeError) as e:
            raise S690Error("s690 回應格式不對") from e
        if not isinstance(data, dict):
            raise S690Error("s690 回應格式不對")
        return data

    def _search(self, query: str, case_type: str) -> dict:
        r = self._rpc("search/fetchSearchId", {"query": query, "options": _options(case_type),
                                                "sortBy": "PD", "sortOrder": "desc"})
        if not isinstance(r.get("total"), int):
            raise S690Error("s690 檢索沒有回筆數")
        return r

    def _page(self, search_id: str, page: int, size: int) -> list:
        lst = self._rpc("search/fetchSearchResultsByPieces",
                        {"searchId": search_id, "page": page, "pageSize": size}).get("list")
        return lst if isinstance(lst, list) else []

    def count(self, case_type: str, day: str) -> int:
        """那一天公開 / 公告的總筆數(690查詢筆數)。"""
        return self._search(f"PD:[{day} TO {day}]", case_type)["total"]

    def image_check(self, case_type: str, vol) -> dict:
        """那一期檢索結果的第一筆有沒有說明書、代表圖。"""
        v, i = split_vol(vol)
        r = self._search(f"OGV:{v} AND OGN:{i}", case_type)
        lst = self._page(r["searchId"], 1, 1) if r["total"] and r.get("searchId") else []
        if not lst:
            return {"pn": None, "spec": False, "rep": False, "detail": f"{v} 卷 {i} 期在 s690 查不到案件"}
        c = lst[0]
        spec = c.get("hasPublishedSpecification" if case_type == "TWA" else "hasIssuedSpecification")
        rep = any(_REP_RE.search(str(f)) for f in c.get("gazetteFigures") or [])
        return {"pn": _pn_of(c), "spec": bool(spec), "rep": rep}

    def sud_sample(self, case_type: str, vol, types: list) -> dict:
        """抽測:這期 log 裡有的雜項類型一次查,取第一筆「雜項真的刊在這一期」的案件判定。

        MS:<卷> AND MS:<期> 不保證兩個值在同一筆雜項上,所以逐筆查雜項明細核對。"""
        v, i = split_vol(vol)
        want = [t for t in types if t != CATALOG.get(case_type)] or list(types)
        if not want:
            return {"pn": None, "type": None, "ok": False, "detail": "log 裡沒有雜項類型,無從抽測"}
        q = f"MS:({' OR '.join(want)}) AND MS:{v} AND MS:{i}"
        r = self._search(q, case_type)
        for page in range(1, MAX_PAGES + 1):
            lst = self._page(r["searchId"], page, PAGE) if r["total"] and r.get("searchId") else []
            for c in lst:
                recs = self._rpc("miscellaneous", {"apn": c.get("apnRaw"), "database": c.get("database")}).get("list")
                rec = next((x for x in recs or [] if x.get("type") in want and _same_issue(x, v, i)), None)
                if rec:
                    return _judge(c, rec)
            if len(lst) < PAGE:
                break
        return {"pn": None, "type": None, "ok": False,
                "detail": f"{q} 前 {MAX_PAGES * PAGE} 筆裡沒有雜項刊在 {v} 卷 {i} 期的案件"}
