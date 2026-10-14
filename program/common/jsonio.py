"""JSON 讀寫。

零專案內相依的葉模組：不 import 專案內任何其他模組，任何地方都能安全使用。

取代原本散在各處的重複實作：
- 原子寫入 6 處（traffic_monitor / storage 等；檢索相關的呼叫端已於 8.0.0 移除）
- 容錯讀 JSON 18 處
"""

import json
import time
from pathlib import Path


def read_json(path, default=None):
    """讀 JSON。讀不到、壞檔、空檔、路徑是目錄——一律回 default，絕不拋例外。

    呼叫端遍布全專案（多半在 Google Drive 同步夾上讀檔，同步中暫時讀不到很常見），
    統一在這裡吞掉，呼叫端就不必各自包 try/except。
    """
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def write_json_atomic(path, obj, retries: int = 4) -> None:
    """先寫 <檔名>.tmp、寫完才 replace() 一次換上。

    直接覆寫在 Google Drive 同步夾上特別危險：同步層可能在寫到一半時讀走，
    留下半截 JSON。原子替換讓讀取端永遠看到完整的舊檔或完整的新檔。

    同步磁碟短暫忙碌時做有限重試；全部失敗才拋出最後一個例外
    （呼叫端要知道沒寫成功，不可默默吞掉）。
    """
    path = Path(path)
    # 先序列化：資料本身有問題時直接拋，不要先動到磁碟上的舊檔
    data = json.dumps(obj, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    last_exc = None
    for attempt in range(max(1, retries)):
        try:
            tmp.write_text(data, encoding="utf-8")
            tmp.replace(path)
            return
        except OSError as e:
            last_exc = e
            time.sleep(0.1 * (attempt + 1))
    try:
        tmp.unlink()
    except OSError:
        pass
    raise last_exc
