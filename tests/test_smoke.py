"""確認測試骨架本身可用：路徑設定正確、暫存目錄可寫可刪。"""

from _util import TempDataCase, ROOT


class TestHarness(TempDataCase):
    def test_root_points_at_project(self):
        self.assertTrue((ROOT / "config.json").exists())
        self.assertTrue((ROOT / "program" / "core" / "traffic_monitor.py").exists())

    def test_tmp_is_writable_and_isolated(self):
        p = self.write_json("x.json", {"a": 1})
        self.assertTrue(p.exists())
        # 確認暫存目錄不在專案內，不會誤動真實資料
        self.assertNotIn(str(ROOT), str(p))

    def test_core_modules_importable(self):
        import paths
        self.assertTrue(paths.BASE.exists())
