"""通知信解析。純函式。

parse_subject 只用 metadata(主旨、Date 標頭)就能建出事件 —— 絕大多數信只走這條,
不必下載全文。parse_raw 在少數需要 log 的信(失敗、期別系統每段第一封、填筆數的步驟)
才用:從附件 .log 取出 RUNDECK:DATA、寫入筆數與錯誤訊息,整份 log 不保存。
"""

import email
import email.policy
import email.utils
import re
from datetime import timedelta, timezone

TPE = timezone(timedelta(hours=8))

_SUBJECT_RE = re.compile(
    r"【(?P<st>[A-Z]+)】\s*\[(?P<proj>[^\]]+)\]\s*(?P<code>\d{6})-(?P<name>\S+)\s*\|\s*(?P<group>\S+)")
_DATA_RE = re.compile(r"^RUNDECK:DATA:(\w+)\s*=\s*(\S+)", re.M)
_WRITES_RE = re.compile(r"\[job_complete\] \S+ .*?writes=(\d+)")
_SUDTYPE_RE = re.compile(r"XML parsing completed \(type=(\w+), recordCount=(\d+)\)")
_ERROR_RE = re.compile(r"^\s*error:\s*(.+)$", re.M)
_EXEC_RE = re.compile(r"#(\d+)")
_URL_RE = re.compile(r'href="([^"]*/execution/show/\d+)"')


def parse_subject(subject: str, date_header: str, msg_id: str, cfg: dict, tz=TPE) -> dict:
    subject = subject or ""
    ev = {"id": msg_id, "code": None, "name": "", "group": "", "status": "unknown",
          "status_word": "", "at": None, "subject": subject, "raw": False}
    try:
        ev["at"] = email.utils.parsedate_to_datetime(date_header).astimezone(tz).isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    m = _SUBJECT_RE.search(subject)
    if not m or m["proj"] != cfg.get("project"):
        return ev
    ev.update(code=m["code"], name=m["name"], group=m["group"], status_word=m["st"],
              status=cfg.get("status_map", {}).get(m["st"], "unknown"))
    return ev


def _part_text(part) -> str:
    """自己從位元組解碼:附件沒標 charset 時,get_content() 會當 ASCII 解,中文全變亂碼
    (「GCS 資料夾無檔案」就比對不到)。有標就照標的,沒標用 UTF-8。"""
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        text = payload.decode(charset, "replace")
    except LookupError:
        text = payload.decode("utf-8", "replace")
    return text.replace("\r\n", "\n")


def parse_raw(raw: bytes, base: dict, cfg: dict) -> dict:
    """base(parse_subject 的結果)加上 log 摘要。"""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    log, html = "", ""
    for part in msg.walk():
        if part.is_multipart():
            continue
        fn = part.get_filename() or ""
        if fn.lower().endswith(".log"):
            if not log:
                log = _part_text(part)
        elif (not html and part.get_content_type() == "text/html"
              and part.get_content_disposition() != "attachment"):
            html = _part_text(part)

    ev = dict(base)
    ev["raw"] = True
    ev["data"] = {k: v for k, v in _DATA_RE.findall(log)}
    hits = _WRITES_RE.findall(log)
    ev["writes"] = int(hits[-1]) if hits else None   # log 是累積的,最後一個才是本步驟
    types = sud_types(log)
    if types:
        ev["sud_types"] = types
    m = _ERROR_RE.search(log)
    ev["error"] = m.group(1).strip()[:200] if m else ""
    m = _EXEC_RE.search(html)
    ev["exec_id"] = int(m.group(1)) if m else None
    m = _URL_RE.search(html)
    ev["url"] = m.group(1) if m else None
    if (ev.get("status") == "fail" and ev["error"] and "step1-raw" in (ev.get("name") or "")
            and re.search(cfg.get("nodata_re") or r"(?!)", ev["error"])):
        ev["status"] = "nodata"
    return ev


def sud_types(log: str) -> dict:
    """{雜項類型: 筆數}:這期有哪些雜項(SUD 抽測用;log 沒有逐筆案號)。"""
    return {t: int(n) for t, n in _SUDTYPE_RE.findall(log or "")}


def log_text(raw: bytes) -> str:
    """原始信 → 附件 .log 的全文(給「查看 log」用,不存檔)。沒有 log 回空字串。"""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    for part in msg.walk():
        if not part.is_multipart() and (part.get_filename() or "").lower().endswith(".log"):
            return _part_text(part)
    return ""
