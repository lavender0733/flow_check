"""690 資轉監控:一次性 Gmail 授權 + 信箱統計。由 690授權.bat 啟動。

1. 用 data/config/gmail_credentials.json(GCP 的 OAuth 用戶端)開瀏覽器授權,只要唯讀權限。
2. token 存到 data/machines/gmail_token_<機器>.json(各機各自授權,不進版控)。
3. 抓近 30 天的 [s690] 信(只取主旨與時間),印出每個代碼的封數、成功失敗、時刻與日期,
   以及屬於 xfer690.json 的哪個系統 —— 拿來校正系統清單的排程與 expect_by。
"""

import collections
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.paths import GMAIL_CREDENTIALS_PATH, GMAIL_TOKEN_PATH, MACHINE_ID
from common.jsonio import write_json_atomic
from xfer690.gmail_client import SCOPES, GmailClient
from xfer690.parser import parse_subject
from xfer690.service import load_xfer_config

SETUP = """找不到 {path}

第一次設定:
  1. 到 https://console.cloud.google.com/ 建專案,啟用 Gmail API。
  2. 「OAuth 同意畫面」選內部(Workspace 帳號),範圍加 gmail.readonly。
  3. 「憑證」→ 建立 OAuth 用戶端 ID → 類型選「電腦版應用程式」→ 下載 JSON。
  4. 把下載的檔案改名為 gmail_credentials.json,放到 data/config/。
  5. 再雙擊一次 690授權.bat。
"""


def authorize() -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(GMAIL_CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(
        port=0, open_browser=True, timeout_seconds=600,
        authorization_prompt_message="授權網址(瀏覽器沒自動開就手動貼上):\n{url}\n",
        success_message="授權完成,可以關掉這個分頁。")
    write_json_atomic(GMAIL_TOKEN_PATH, json.loads(creds.to_json()))
    print(f"授權完成,token 存在 {GMAIL_TOKEN_PATH}")


def stats(cfg: dict) -> None:
    owner = {}
    for s in cfg["systems"]:
        for c in s["auto"] + s["steps"]:
            owner[c] = s["id"]
    client = GmailClient(GMAIL_TOKEN_PATH)
    ids = client.list_ids(f"({cfg['gmail_query']}) newer_than:30d")
    print(f"\n近 30 天 {len(ids)} 封,逐封讀主旨中…")
    rows = collections.defaultdict(lambda: {"name": "", "st": collections.Counter(),
                                            "times": [], "days": set()})
    total = 0
    for mid in ids:
        meta = client.fetch_meta(mid)
        total += meta["size"]
        ev = parse_subject(meta["subject"], meta["date"], mid, cfg)
        if not ev["code"] or not ev["at"]:
            continue
        r = rows[ev["code"]]
        r["name"] = ev["name"]
        r["st"][ev["status_word"]] += 1
        r["times"].append(ev["at"][11:16])
        r["days"].add(ev["at"][5:10])
    print(f"合計 {total / 1024 / 1024:.1f} MB(sizeEstimate;只抓主旨不會下載這些)\n")
    print(f"{'代碼':<7} {'名稱':<40} {'封':>3}  {'狀態':<22} {'時刻 最早/中位/最晚':<20} {'系統':<11} 日期")
    for code in sorted(rows):
        r = rows[code]
        t = sorted(r["times"])
        st = ", ".join(f"{k}:{v}" for k, v in sorted(r["st"].items()))
        days = sorted(r["days"])
        dd = "、".join(days) if len(days) <= 6 else f"{days[0]}~{days[-1]} 共 {len(days)} 天"
        print(f"{code:<7} {r['name'][:40]:<40} {sum(r['st'].values()):>3}  {st:<22} "
              f"{t[0]}/{t[len(t) // 2]}/{t[-1]:<8} {owner.get(code, '—'):<11} {dd}")


def main() -> int:
    print(f"690 資轉監控 — Gmail 授權(本機 {MACHINE_ID})\n")
    if not GMAIL_CREDENTIALS_PATH.exists():
        print(SETUP.format(path=GMAIL_CREDENTIALS_PATH))
        return 1
    try:
        import google_auth_oauthlib  # noqa: F401
    except ImportError:
        print("缺少套件,請先執行:python -m pip install -r requirements-690.txt")
        return 1
    authorize()
    stats(load_xfer_config())
    return 0


if __name__ == "__main__":
    sys.exit(main())
