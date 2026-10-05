"""測試用:組出跟真實 s690 通知信同一個形狀的原始信。

真實信的樣子(2026-10-01 讀信箱確認):主旨 RFC 2047 編碼、帶 emoji;內文是 HTML,
有執行編號 #2177 與 Rundeck 的 execution/show 連結;log 是附檔名 .log 的附件。
"""

from email.message import EmailMessage
from pathlib import Path

FIX = Path(__file__).resolve().parent / "fixtures" / "xfer690"


def fixture_log(code: str) -> str:
    return (FIX / f"{code}.log").read_text(encoding="utf-8")


def subject(status: str, code: str, name: str, group: str = "01-tw/20-x") -> str:
    emoji = "🟢" if status == "SUCCESS" else "🔴"
    return f"{emoji}【{status}】[s690] {code}-{name} | {group}"


def make_eml(subj: str, html: str = None, log: str = None, exec_id: int = 2177,
             date: str = "Thu, 01 Oct 2026 02:10:02 +0800") -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subj
    msg["From"] = "rundeck@tipo.innovue.ltd"
    msg["To"] = "tipo@innovue.ltd"
    msg["Date"] = date
    if html is None:
        html = (f'<html><body><p>Execution <b>#{exec_id}</b></p>'
                f'<a href="http://192.168.88.102:4440/project/s690/execution/show/{exec_id}">'
                f'View Output</a></body></html>')
    msg.set_content("plain")
    msg.add_alternative(html, subtype="html")
    if log is not None:
        msg.add_attachment(log.encode("utf-8"), maintype="text", subtype="plain",
                           filename=f"{exec_id}.log")
    return msg.as_bytes()
