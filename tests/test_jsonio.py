"""共用 JSON 讀寫與檔案簽章。"""

import json
import time

from _util import TempDataCase

from common import jsonio


class TestReadJson(TempDataCase):
    def test_reads_valid_json(self):
        p = self.write_json("a.json", {"k": 1})
        self.assertEqual(jsonio.read_json(p), {"k": 1})

    def test_missing_file_returns_default(self):
        self.assertIsNone(jsonio.read_json(self.tmp / "nope.json"))
        self.assertEqual(jsonio.read_json(self.tmp / "nope.json", default=[]), [])

    def test_corrupt_file_returns_default(self):
        p = self.tmp / "bad.json"
        p.write_text("{ not json", encoding="utf-8")
        self.assertEqual(jsonio.read_json(p, default={}), {})

    def test_empty_file_returns_default(self):
        p = self.tmp / "empty.json"
        p.write_text("", encoding="utf-8")
        self.assertEqual(jsonio.read_json(p, default={"fallback": True}),
                         {"fallback": True})

    def test_directory_returns_default(self):
        d = self.tmp / "adir"
        d.mkdir()
        self.assertEqual(jsonio.read_json(d, default=0), 0)

    def test_never_raises(self):
        """呼叫端遍布全專案，這個函式必須永不拋例外。"""
        for bad in (self.tmp / "nope.json", self.tmp, None, ""):
            jsonio.read_json(bad, default=None)


class TestWriteJsonAtomic(TempDataCase):
    def test_writes_and_reads_back(self):
        p = self.tmp / "out.json"
        jsonio.write_json_atomic(p, {"k": "值"})
        self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"k": "值"})

    def test_creates_parent_directories(self):
        p = self.tmp / "deep" / "nested" / "out.json"
        jsonio.write_json_atomic(p, [1, 2, 3])
        self.assertTrue(p.exists())

    def test_no_temp_file_left_behind(self):
        p = self.tmp / "out.json"
        jsonio.write_json_atomic(p, {"k": 1})
        self.assertEqual(list(self.tmp.glob("*.tmp")), [])

    def test_non_ascii_not_escaped(self):
        """檔案要人看得懂：中文不可被跳脫成 \\uXXXX。"""
        p = self.tmp / "out.json"
        jsonio.write_json_atomic(p, {"機器": "桌機"})
        self.assertIn("桌機", p.read_text(encoding="utf-8"))

    def test_overwrite_replaces_content(self):
        p = self.tmp / "out.json"
        jsonio.write_json_atomic(p, {"v": 1})
        jsonio.write_json_atomic(p, {"v": 2})
        self.assertEqual(jsonio.read_json(p), {"v": 2})

    def test_existing_file_survives_when_write_fails(self):
        """寫入失敗時不可留下半截檔，舊內容必須完好。"""
        p = self.tmp / "out.json"
        jsonio.write_json_atomic(p, {"good": True})

        class Unserialisable:
            pass

        with self.assertRaises(TypeError):
            jsonio.write_json_atomic(p, {"bad": Unserialisable()})
        self.assertEqual(jsonio.read_json(p), {"good": True})
        self.assertEqual(list(self.tmp.glob("*.tmp")), [])


class TestFileSig(TempDataCase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(jsonio.file_sig(self.tmp / "nope.json"), "")

    def test_same_file_same_signature(self):
        p = self.write_json("a.json", {"k": 1})
        self.assertEqual(jsonio.file_sig(p), jsonio.file_sig(p))

    def test_signature_changes_when_content_changes(self):
        p = self.write_json("a.json", {"k": 1})
        before = jsonio.file_sig(p)
        time.sleep(0.01)
        p.write_text(json.dumps({"k": 2, "more": "padding"}), encoding="utf-8")
        self.assertNotEqual(before, jsonio.file_sig(p))

    def test_version_suffix_matches_yearly_server_format(self):
        """必須逐字相容 yearly_server 已寫進磁碟的快取格式
        f"{size}:{mtime_ns}:v{CACHE_VERSION}"，否則既有快取全部失效。"""
        p = self.write_json("a.json", {"k": 1})
        st = p.stat()
        self.assertEqual(jsonio.file_sig(p, version=2),
                         f"{st.st_size}:{st.st_mtime_ns}:v2")

    def test_no_version_has_no_suffix(self):
        p = self.write_json("a.json", {"k": 1})
        st = p.stat()
        self.assertEqual(jsonio.file_sig(p), f"{st.st_size}:{st.st_mtime_ns}")

    def test_different_version_gives_different_signature(self):
        p = self.write_json("a.json", {"k": 1})
        self.assertNotEqual(jsonio.file_sig(p, version=1),
                            jsonio.file_sig(p, version=2))


class TestSigCache(TempDataCase):
    def test_first_call_parses_second_call_hits(self):
        p = self.write_json("a.json", {"k": 1})
        calls = []

        def parse(path):
            calls.append(path)
            return "parsed"

        cache = jsonio.SigCache()
        data, hit = cache.get(p, parse)
        self.assertEqual(data, "parsed")
        self.assertFalse(hit)

        data, hit = cache.get(p, parse)
        self.assertEqual(data, "parsed")
        self.assertTrue(hit)
        self.assertEqual(len(calls), 1)

    def test_changed_file_reparses(self):
        p = self.write_json("a.json", {"k": 1})
        calls = []

        def parse(path):
            calls.append(path)
            return p.read_text(encoding="utf-8")

        cache = jsonio.SigCache()
        cache.get(p, parse)
        time.sleep(0.01)
        p.write_text(json.dumps({"k": 2, "pad": "xxxx"}), encoding="utf-8")
        _, hit = cache.get(p, parse)
        self.assertFalse(hit)
        self.assertEqual(len(calls), 2)

    def test_version_bump_invalidates(self):
        """解析結果的欄位改版後，舊快取的形狀對不上 -> 換 version 讓它自動失效。"""
        p = self.write_json("a.json", {"k": 1})
        parse = lambda path: "v1-shape"

        c1 = jsonio.SigCache(version=1)
        c1.get(p, parse)

        c2 = jsonio.SigCache(version=2)
        _, hit = c2.get(p, parse)
        self.assertFalse(hit)

    def test_clear_forces_reparse(self):
        p = self.write_json("a.json", {"k": 1})
        parse = lambda path: "x"
        cache = jsonio.SigCache()
        cache.get(p, parse)
        cache.clear()
        _, hit = cache.get(p, parse)
        self.assertFalse(hit)

    def test_missing_file_is_not_cached_as_a_hit(self):
        missing = self.tmp / "nope.json"
        parse = lambda path: "default"
        cache = jsonio.SigCache()
        cache.get(missing, parse)
        _, hit = cache.get(missing, parse)
        self.assertFalse(hit)
