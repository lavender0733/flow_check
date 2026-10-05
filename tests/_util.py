"""測試共用工具。

所有測試都在 tempfile 目錄中執行，絕不觸碰真實的 data/。
"""

import sys
import shutil
import tempfile
import unittest
from pathlib import Path

# 專案根目錄（tests/ 的上一層）
ROOT = Path(__file__).resolve().parents[1]


def add_source_paths() -> None:
    """把 program/ 加進 sys.path，讓測試以套件路徑 import（與正式程式一致）。

    冪等：重複呼叫不會重複插入。
    """
    p = str(ROOT / "program")
    if p not in sys.path:
        sys.path.insert(0, p)


# 不要在 Google Drive 同步夾裡寫 .pyc（與正式程式一致）
sys.dont_write_bytecode = True
add_source_paths()


class TempDataCase(unittest.TestCase):
    """提供一個乾淨的暫存目錄 self.tmp，測試結束自動刪除。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flowcheck-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_json(self, name: str, obj) -> Path:
        """在暫存目錄寫一個 JSON 檔，回傳路徑。"""
        import json
        p = self.tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        return p
