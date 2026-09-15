"""JSON 讀寫與檔案簽章快取。

零專案內相依的葉模組：不 import 專案內任何其他模組，任何地方都能安全使用。

取代原本散在各處的重複實作：
- 原子寫入 6 處（traffic_monitor / storage / monthly_progress / output_store ×2 / yearly_server）
- 容錯讀 JSON 18 處
- 檔案簽章 3 處（格式原本不一致，見下方 file_sig 的說明）
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


def file_sig(path, version=None) -> str:
    """檔案的輕量變動簽章（大小 + 修改時間奈秒）；讀不到回空字串。

    格式為 "<size>:<mtime_ns>"，帶 version 時附加 ":v<version>"。
    帶 version 的形式與 yearly_server 原本寫進 data/年度統計快取.json 的格式
    逐字相同，因此既有的磁碟快取不會失效。

    用 mtime_ns 而非截斷到秒的 mtime：同一秒內的修改也抓得到。
    """
    try:
        st = Path(path).stat()
    except (OSError, TypeError):
        return ""
    base = f"{st.st_size}:{st.st_mtime_ns}"
    return f"{base}:v{version}" if version is not None else base


class SigCache:
    """以檔案簽章為鍵的解析結果快取。

    用途：Excel / 匯出檔的解析很貴，但檔案只在使用者換新檔時才變。
    簽章沒變就回上次的解析結果。

    version 用於「解析結果的欄位改版」：欄位一改，舊快取的形狀就對不上，
    換一個 version 即可讓所有舊項目自動失效，不必手動刪快取檔。
    """

    def __init__(self, version=None):
        self.version = version
        self._items = {}     # str(path) -> (sig, data)

    def get(self, path, parse):
        """簽章命中就回快取；否則呼叫 parse(path) 重新解析。

        回傳 (data, hit)。hit 為 True 代表這次沒有重新解析。
        檔案讀不到時（簽章為空）一律視為未命中，不快取。
        """
        key = str(path)
        sig = file_sig(path, self.version)
        if sig:
            cached = self._items.get(key)
            if cached and cached[0] == sig:
                return cached[1], True
        data = parse(path)
        if sig:
            self._items[key] = (sig, data)
        return data, False

    def clear(self) -> None:
        self._items.clear()
