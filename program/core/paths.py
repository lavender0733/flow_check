"""
路徑與機器身分常數
==================
從 traffic_monitor.py 抽出的葉模組(只相依 common.ident)。所有使用者資料放在專案根目錄
flow_check/data/，並依用途分成 config(共用設定)與 machines(各機執行期資料)。
各機存檔/偏好檔名帶上機器代號,Google Drive 同步時才不會互相覆蓋。
"""

import os
import tempfile
from pathlib import Path

from common.ident import machine_id

BASE = Path(__file__).resolve().parents[2]
DATA_DIR = BASE / "data"
DATA_CONFIG_DIR = DATA_DIR / "config"
MACHINE_DATA_DIR = DATA_DIR / "machines"
CONFIG_PATH = BASE / "config.json"
MACHINES_PATH = DATA_CONFIG_DIR / "machines.json"
WEB_DIR = BASE / "web"

MACHINE_ID = machine_id()
STATE_PATH = MACHINE_DATA_DIR / f"state_{MACHINE_ID}.json"   # 流量存檔各機獨立
# 網頁 UI 偏好(用量通知開關/門檻等)各機獨立。原本只存瀏覽器 localStorage,
# 但瀏覽器關閉清資料/換瀏覽器就會掉 —— 改存到這個檔,重開一定留得住(各台自己記自己的)。
PREFS_PATH = MACHINE_DATA_DIR / f"prefs_{MACHINE_ID}.json"
# 流量逐分鐘明細(每次取樣一行 JSONL,一天一檔)。刻意放本機 LOCALAPPDATA、不放 data/machines:
# 這個檔每分鐘都在長,放在 Google Drive 同步夾裡會讓 Drive 每分鐘重傳整個檔 —— 到傍晚一個檔
# 幾百 KB、一天重傳上千次,監控工具自己就成了吃流量的元兇。它是各機自己的診斷紀錄,本來就不必同步。
TRAFFIC_LOG_DIR = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "flow_check" / "traffic"
# 每日用量統計(長期保存,供流量報表)。一天只寫一次,放在同步夾裡沒關係。
DAILY_STATS_PATH = MACHINE_DATA_DIR / f"daily_stats_{MACHINE_ID}.json"

# 690 資轉監控(program/xfer690)。規則設定進版控;另外三個是機密,不進版控(見 .gitignore)。
XFER690_CONFIG_PATH = DATA_CONFIG_DIR / "xfer690.json"
XFER690_SECRETS_PATH = DATA_CONFIG_DIR / "xfer690_secrets.json"   # wiki 登入憑證(wiki_jwt)
GMAIL_CREDENTIALS_PATH = DATA_CONFIG_DIR / "gmail_credentials.json"   # GCP 的 OAuth 用戶端
GMAIL_TOKEN_PATH = MACHINE_DATA_DIR / f"gmail_token_{MACHINE_ID}.json"   # 各機各自授權
XFER690_CACHE_PATH = MACHINE_DATA_DIR / f"xfer690_{MACHINE_ID}.json"   # 抓過的信(事件)

