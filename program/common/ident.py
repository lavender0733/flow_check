"""機器身分。

零專案內相依的葉模組。取代原本重複的四份 machine_id()。
"""

import re
import socket


def machine_id() -> str:
    """本機代號：主機名稱過濾成檔名安全字元。

    這個值會嵌進檔名（state_<機器>.json、prefs_<機器>.json 等），
    讓 Google Drive 同步時各機不互相覆蓋。身分只能取自主機名稱——
    同步資料夾裡的檔案兩台看到的都一樣。
    """
    raw = socket.gethostname() or "PC"
    mid = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-")
    return mid or "PC"
