"""
路徑與機器身分常數
==================
從 traffic_monitor.py 抽出的葉模組(只相依 common.ident)。所有使用者資料放在專案根目錄
flow_check/data/，並依用途分成 config(共用設定)、machines(各機執行期資料)與
exports(手動下載匯出檔)。各機存檔/偏好檔名帶上機器代號,Google Drive 同步時才不會
互相覆蓋。
"""

from pathlib import Path

from common.ident import machine_id

BASE = Path(__file__).resolve().parents[2]
DATA_DIR = BASE / "data"
DATA_CONFIG_DIR = DATA_DIR / "config"
MACHINE_DATA_DIR = DATA_DIR / "machines"
EXPORTS_DIR = DATA_DIR / "exports"
CONFIG_PATH = BASE / "config.json"
MACHINES_PATH = DATA_CONFIG_DIR / "machines.json"
HOLIDAYS_PATH = DATA_CONFIG_DIR / "holidays.json"
TARGETS_PATH = DATA_CONFIG_DIR / "targets.json"
API_QUOTA_BASELINE_PATH = DATA_CONFIG_DIR / "api_quota_baseline.json"
WEB_DIR = BASE / "web"

MACHINE_ID = machine_id()
STATE_PATH = MACHINE_DATA_DIR / f"state_{MACHINE_ID}.json"   # 流量存檔各機獨立
# 網頁 UI 偏好(用量通知開關/門檻等)各機獨立。原本只存瀏覽器 localStorage,
# 但瀏覽器關閉清資料/換瀏覽器就會掉 —— 改存到這個檔,重開一定留得住(各台自己記自己的)。
PREFS_PATH = MACHINE_DATA_DIR / f"prefs_{MACHINE_ID}.json"
RUN_LOG_PATH = MACHINE_DATA_DIR / f"run_log_{MACHINE_ID}.jsonl"
HISTORY_PATH = MACHINE_DATA_DIR / f"monthly_progress_history_{MACHINE_ID}.json"
