"""690 資轉監控的組裝:設定、抓信、快取、狀態、wiki。"""

import copy

from common.jsonio import read_json
from core.paths import XFER690_CONFIG_PATH

# 預設值。systems / tables 留空:實際的系統清單在 data/config/xfer690.json(進版控)。
DEFAULT_XFER_CONFIG = {
    "enabled": True,
    "poll_interval_min": 10,
    "retain_days": 45,
    "history_days": 14,
    "project": "s690",
    "gmail_query": 'list:tipo.innovue.ltd subject:"[s690]"',
    "status_map": {"SUCCESS": "ok", "FAILURE": "fail"},
    "nodata_re": "GCS 資料夾無檔案",
    "max_raw_kb": 512,
    "systems": [],
    "wiki": {
        "enabled": False,
        "dry_run": True,
        "url": "https://km.innovue.ltd",
        "locale": "zh-tw",
        "days": 14,
        "placeholder_re": r"^\d{4}x{4}$",
        "base": "QC部/40_其他專案系統/智慧局-690整合式專利檢索系統/資料轉置登記",
        "tables": [],
    },
}


def load_xfer_config(path=XFER690_CONFIG_PATH) -> dict:
    """預設值合併檔案內容。wiki 子物件做一層合併;陣列直接取代。讀不到或壞檔回預設值。"""
    cfg = copy.deepcopy(DEFAULT_XFER_CONFIG)
    data = read_json(path, default={})
    if not isinstance(data, dict):
        return cfg
    for k, v in data.items():
        if k == "wiki" and isinstance(v, dict):
            cfg["wiki"].update(v)
        else:
            cfg[k] = v
    return cfg
