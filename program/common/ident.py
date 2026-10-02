"""機器身分與驗證碼遮罩。

零專案內相依的葉模組。取代原本重複的四份 machine_id() 與三份 mask_code()。
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


def mask_code(code) -> str:
    """只顯示前 4 碼，其餘以 * 取代（保留長度）。

    統一自三份實作；採用其中「有 strip」的版本——額度卡與月進度卡
    兩張都用它，顯示必須對齊。
    """
    code = str(code or "").strip()
    return code if len(code) <= 4 else code[:4] + "*" * (len(code) - 4)
