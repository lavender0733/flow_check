"""Wiki.js 頁面裡 markdown 表格的編輯:找表、讀列、只填空格、插列。純函式。

人也在編輯同一頁,所以規則很保守:
- 只寫目標格,而且只在格子是空的(或是 2026xxxx 這種佔位字)時才寫;值不同列為衝突、不寫。
- 欄名對不上時整張表不寫(表格被人改過結構,欄號可能已經錯位)。
- 目標列以外的每一行逐位元組不變,包括行尾字元。
"""

import re


class TableNotFound(Exception):
    pass


class LabelMismatch(Exception):
    pass


_SEP_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")


def _body(line: str) -> tuple:
    """(內容, 行尾)。"""
    stripped = line.rstrip("\r\n")
    return stripped, line[len(stripped):]


def _cell_spans(body: str) -> list:
    """每一格在 body 裡的 (起, 迄)。去掉頭尾的 |,依沒有跳脫的 | 切開。"""
    pipes = [m.start() for m in re.finditer(r"(?<!\\)\|", body)]
    if not pipes:
        return [(0, len(body))]
    starts = pipes
    ends = pipes[1:]
    if body.rstrip().endswith("|") and len(pipes) > 1:
        starts = pipes[:-1]
    else:
        ends = pipes[1:] + [len(body)]
    return [(s + 1, e) for s, e in zip(starts, ends)]


def split_row(line: str) -> list:
    body, _ = _body(line)
    return [body[s:e] for s, e in _cell_spans(body)]


def _is_row(line: str) -> bool:
    return line.lstrip().startswith("|")


def _numeric(*keys) -> bool:
    return all(str(k).strip().isdigit() for k in keys)


def _older(a, b) -> bool:
    """a 比 b 舊(列鍵小)。"""
    if _numeric(a, b):
        return int(a) < int(b)
    return str(a) < str(b)


def _fit(orig: str, value: str) -> str:
    """把 value 放進一格,保持原本的寬度:左右至少各一個空白,不夠就撐開,置中。"""
    room = len(orig) - len(value)
    left = max(1, room // 2)
    right = max(1, room - left)
    return " " * left + value + " " * right


def locate(lines: list, target: dict) -> dict:
    """找表。回傳 {"header", "labels"(欄名列的格), "label_idx", "rows": [行號…]}。"""
    after = target.get("after") or ""
    start = 0
    if after:
        for i, line in enumerate(lines):
            if after in line:
                start = i + 1
                break
        else:
            raise TableNotFound(f"找不到「{after}」")
    in_comment = False
    header = None
    for i in range(start, len(lines)):
        line = lines[i]
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        if "<!--" in line:
            if "-->" not in line.split("<!--", 1)[1]:
                in_comment = True
            continue
        if (_is_row(line) and all(h in line for h in target.get("header_has", []))
                and i + 1 < len(lines) and _SEP_RE.match(_body(lines[i + 1])[0])):
            header = i
            break
    if header is None:
        raise TableNotFound("找不到表頭含 " + "、".join(target.get("header_has", [])) + " 的表格")
    first = header + 2
    if target.get("label_row") == "first":
        if first >= len(lines) or not _is_row(lines[first]):
            raise TableNotFound("表頭下面沒有欄名列")
        label_idx = first
        first += 1
    else:
        label_idx = header
    rows = []
    i = first
    while i < len(lines) and _is_row(lines[i]):
        rows.append(i)
        i += 1
    return {"header": header, "label_idx": label_idx,
            "labels": [c.strip() for c in split_row(lines[label_idx])], "rows": rows}


def _check_labels(table: dict, target: dict) -> None:
    """整列欄名都要對上:每日表 RAW/L3/OS 重複四次,只看自己那幾欄抓不到別處插欄造成的錯位。"""
    labels = table["labels"]
    want = target.get("labels")
    if want is not None and labels != list(want):
        raise LabelMismatch("欄名列應為「" + " | ".join(want) + "」,頁面上是「"
                            + " | ".join(labels) + "」")
    for col, c in (target.get("cols") or {}).items():
        col = int(col)
        got = labels[col] if col < len(labels) else None
        if got != c.get("label"):
            raise LabelMismatch(f"第 {col} 欄應為「{c.get('label')}」,頁面上是「{got}」")


def _set_cell(line: str, col: int, value: str) -> str:
    body, eol = _body(line)
    s, e = _cell_spans(body)[col]
    return body[:s] + _fit(body[s:e], value) + body[e:] + eol


def _key_of(line: str, key_col: int):
    cells = split_row(line)
    return cells[key_col].strip() if key_col < len(cells) else None


def apply_fills(content: str, target: dict, rows: dict, placeholder_re: str = "") -> tuple:
    """依 rows({列鍵: {"cells": {欄: 值}, "new": {欄: 值}}})填格。回傳 (新內容, report)。"""
    lines = content.splitlines(keepends=True)
    table = locate(lines, target)
    _check_labels(table, target)
    ph = re.compile(placeholder_re) if placeholder_re else None
    key_col = int(target.get("key_col", 0))
    report = {"filled": 0, "inserted": 0, "skipped": [], "conflicts": [], "rows": []}

    by_key = {}
    for i in table["rows"]:
        k = _key_of(lines[i], key_col)
        if k:
            by_key.setdefault(k, i)

    def fill(i, key, cells, detail):
        for col, val in sorted(((int(c), str(v)) for c, v in cells.items())):
            spans = split_row(lines[i])
            if col >= len(spans):
                report["conflicts"].append({"key": key, "col": col, "existing": None, "wanted": val})
                detail[col] = {"value": val, "existing": None, "state": "conflict"}
                continue
            cur = spans[col].strip()
            if cur == val:
                detail[col] = {"value": val, "existing": cur, "state": "same"}
            elif cur == "" or (ph and ph.search(cur)):
                lines[i] = _set_cell(lines[i], col, val)
                report["filled"] += 1
                detail[col] = {"value": val, "existing": cur, "state": "fill"}
            else:
                report["conflicts"].append({"key": key, "col": col, "existing": cur, "wanted": val})
                detail[col] = {"value": val, "existing": cur, "state": "conflict"}

    # 已存在的列先填(插列會移動行號)
    for key, spec in rows.items():
        key = str(key)
        if key in by_key:
            detail = {}
            fill(by_key[key], key, spec.get("cells") or {}, detail)
            report["rows"].append({"key": key, "new": False, "cells": detail,
                                   "row": [c.strip() for c in split_row(lines[by_key[key]])]})

    missing = [str(k) for k in rows if str(k) not in by_key]
    missing.sort(key=lambda k: (int(k) if k.isdigit() else 0, k), reverse=True)
    width = len(table["labels"])
    for key in missing:
        data = [(i, _key_of(lines[i], key_col)) for i in table["rows"]]
        data = [(i, k) for i, k in data if k]
        pos = next((i for i, k in data if _older(k, key)), None)
        if pos is None:
            report["skipped"].append(key)
            continue
        spec = rows[key] if key in rows else rows[int(key)]
        cells = [""] * width
        for col, val in (spec.get("new") or {}).items():
            if int(col) < width:
                cells[int(col)] = str(val)
        detail = {}
        for col, val in (spec.get("cells") or {}).items():
            if int(col) < width:
                cells[int(col)] = str(val)
                report["filled"] += 1
                detail[int(col)] = {"value": str(val), "existing": "", "state": "fill"}
        eol = _body(lines[pos])[1] or "\n"
        lines.insert(pos, "| " + " | ".join(cells) + " |" + eol)
        table["rows"] = [r + 1 if r >= pos else r for r in table["rows"]] + [pos]
        table["rows"].sort()
        report["inserted"] += 1
        report["rows"].append({"key": key, "new": True, "cells": detail, "row": cells})
    return "".join(lines), report


def read_rows(content: str, target: dict) -> dict:
    """{列鍵: [各格去空白後的值]}(預覽用)。"""
    lines = content.splitlines(keepends=True)
    table = locate(lines, target)
    key_col = int(target.get("key_col", 0))
    out = {}
    for i in table["rows"]:
        k = _key_of(lines[i], key_col)
        if k and k not in out:
            out[k] = [c.strip() for c in split_row(lines[i])]
    return {"labels": table["labels"], "rows": out}


def apply_edits(content: str, target: dict, edits: list) -> tuple:
    """使用者在畫面上明確改過的格(唯一會覆寫非空值的路徑)。

    edits: [{"key", "col"(欄號), "expect", "value"}]。那一格的現值跟 expect 不同 → 整筆不寫,
    列為衝突(別人剛改過,要使用者重新看)。欄名對不上 → LabelMismatch,整張表不寫。
    回傳 (新內容, {"edited", "conflicts"})。
    """
    lines = content.splitlines(keepends=True)
    table = locate(lines, target)
    _check_labels(table, target)
    key_col = int(target.get("key_col", 0))
    by_key = {}
    for i in table["rows"]:
        k = _key_of(lines[i], key_col)
        if k:
            by_key.setdefault(k, i)
    report = {"edited": 0, "conflicts": []}
    for e in edits:
        key, col = str(e.get("key")), int(e.get("col"))
        i = by_key.get(key)
        cells = split_row(lines[i]) if i is not None else []
        cur = cells[col].strip() if col < len(cells) else None
        if cur is None or cur != str(e.get("expect", "")).strip():
            report["conflicts"].append({"key": key, "col": col, "existing": cur,
                                        "expect": e.get("expect"), "wanted": e.get("value")})
            continue
        val = str(e.get("value", "")).strip()
        if val != cur:
            lines[i] = _set_cell(lines[i], col, val) if val else _clear_cell(lines[i], col)
            report["edited"] += 1
    return "".join(lines), report


def _clear_cell(line: str, col: int) -> str:
    body, eol = _body(line)
    s, e = _cell_spans(body)[col]
    return body[:s] + " " * max(1, e - s) + body[e:] + eol
