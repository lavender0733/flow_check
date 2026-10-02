"""同步腳本(tools/sync_tipo.py)的測試。

路徑替換錯了會直接毀掉同事的資料,所以這支腳本例外要寫測試(其他程式靠 Playwright 驗)。
全部在暫存資料夾裡做,不碰真正的 TIPO flow check。

執行:py -3.13 -m unittest tools.test_sync_tipo -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True

from tools import sync_tipo  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

# TIPO flow check 現有的 config.json(2026-10-02 讀到的那份),原封不動。
# 沒有新版用到的鍵(例如 report_apps),用來確認同事不改設定也能升級。
OLD_TIPO_CONFIG = {
    "quota_gb": 2.0,
    "port": 8765,
    "sample_interval_sec": 60,
    "lan_probe_interval_sec": 1,
    "exclude_nics": ["Loopback", "isatap", "Teredo"],
    "exclude_lan": True,
    "lan_ranges": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"],
    "count_ranges": ["10.65.0.0/16"],
    "local_ranges": ["127.0.0.0/8", "::1/128"],
    "speed_floor_mbps": 5.0,
    "peak_window_min": 10,
    "speedtest_url": "https://speed.cloudflare.com/__down",
    "speedtest_bytes": 8388608,
    "speedtest_timeout": 6,
}


def load_spec() -> dict:
    return json.loads((REPO / "tools" / "sync_tipo.json").read_text(encoding="utf-8"))


def copy_repo(dst: Path) -> Path:
    """把 build 會讀到的來源複製一份到暫存資料夾,讓測試可以隨意改它。"""
    for name in ("program", "web", "doc"):
        shutil.copytree(REPO / name, dst / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "plans", "specs"))
    for f in REPO.iterdir():
        if f.is_file() and f.suffix in (".bat", ".txt"):
            shutil.copy2(f, dst / f.name)
    return dst


def machine_id() -> str:
    sys.path.insert(0, str(REPO / "program"))
    try:
        from common.ident import machine_id as mid
        return mid()
    finally:
        sys.path.remove(str(REPO / "program"))


class SyncTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class BuildTest(SyncTestCase):
    def test_paths_rewritten_to_tipo_layout(self):
        out = self.tmp / "out"
        sync_tipo.build(REPO, load_spec(), out)
        code = (
            "import sys, json; sys.dont_write_bytecode = True; sys.path.insert(0, sys.argv[1]);"
            "import core.paths as p;"
            "print(json.dumps({k: str(getattr(p, k)) for k in"
            " ('DATA_DIR', 'STATE_PATH', 'PREFS_PATH', 'MACHINES_PATH', 'DAILY_STATS_PATH',"
            "  'TRAFFIC_LOG_DIR', 'MACHINE_ID')}))"
        )
        r = subprocess.run([sys.executable, "-c", code, str(out / "program")],
                           capture_output=True, text=True, encoding="utf-8", check=True)
        p = {k: Path(v) if k != "MACHINE_ID" else v for k, v in json.loads(r.stdout).items()}
        data = out / "data"
        self.assertEqual(p["DATA_DIR"], data)
        self.assertEqual(p["STATE_PATH"], data / f"state_{p['MACHINE_ID']}.json")
        self.assertEqual(p["PREFS_PATH"], data / f"prefs_{p['MACHINE_ID']}.json")
        self.assertEqual(p["MACHINES_PATH"], data / "machines.json")
        self.assertEqual(p["DAILY_STATS_PATH"], data / f"daily_stats_{p['MACHINE_ID']}.json")
        self.assertEqual(p["TRAFFIC_LOG_DIR"].parts[-2:], ("tipo_flow_check", "traffic"))
        bat = (out / "start_monitor.bat").read_text(encoding="utf-8")
        self.assertIn('set "TASKNAME=TipoFlowCheckTrafficMonitor"', bat)
        self.assertNotIn('set "TASKNAME=FlowCheckTrafficMonitor"', bat)

    def test_rewrite_fails_loudly_when_pattern_missing(self):
        src = copy_repo(self.tmp / "src")
        pp = src / "program" / "core" / "paths.py"
        old = 'STATE_PATH = MACHINE_DATA_DIR / f"state_{MACHINE_ID}.json"'
        pp.write_text(pp.read_text(encoding="utf-8").replace(old, "STATE_PATH = None"), encoding="utf-8")
        with self.assertRaises(sync_tipo.RewriteError) as cm:
            sync_tipo.build(src, load_spec(), self.tmp / "out")
        self.assertIn(old, str(cm.exception))
        self.assertEqual(cm.exception.count, 0)

    def test_rewrite_fails_when_pattern_twice(self):
        src = copy_repo(self.tmp / "src")
        pp = src / "program" / "core" / "paths.py"
        old = 'PREFS_PATH = MACHINE_DATA_DIR / f"prefs_{MACHINE_ID}.json"'
        pp.write_text(pp.read_text(encoding="utf-8") + "\n# " + old + "\n", encoding="utf-8")
        with self.assertRaises(sync_tipo.RewriteError) as cm:
            sync_tipo.build(src, load_spec(), self.tmp / "out")
        self.assertIn(old, str(cm.exception))
        self.assertEqual(cm.exception.count, 2)

    def test_no_xfer690_or_secrets_in_output(self):
        src = copy_repo(self.tmp / "src")
        (src / "program" / "xfer690").mkdir()
        (src / "program" / "xfer690" / "a.py").write_text("x = 1\n", encoding="utf-8")
        (src / "data" / "config").mkdir(parents=True)
        (src / "data" / "config" / "gmail_credentials.json").write_text("{}", encoding="utf-8")
        out = self.tmp / "out"
        sync_tipo.build(src, load_spec(), out)
        files = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
        self.assertFalse([f for f in files if "xfer690" in f], files)
        self.assertFalse([f for f in files if f.startswith("data/") or "gmail" in f], files)
        self.assertIn("program/core/traffic_monitor.py", files)

    def test_readme_copied(self):
        out = self.tmp / "out"
        sync_tipo.build(REPO, load_spec(), out)
        self.assertEqual((out / "README.md").read_bytes(),
                         (REPO / "doc" / "同事版README.md").read_bytes())


class PlanApplyTest(SyncTestCase):
    def _staged(self) -> Path:
        out = self.tmp / "staged"
        sync_tipo.build(REPO, load_spec(), out)
        return out

    def test_never_touches_data_and_config(self):
        staged = self._staged()
        dst = self.tmp / "dst"
        keep = {
            "data/state_X.json": b'{"date": "2026-10-02", "day_recv": 1}',
            "config.json": b'{"port": 8765}',
            "desktop.ini": b"[.ShellClassInfo]",
            "web/desktop.ini": b"[.ShellClassInfo] web",
        }
        for rel, body in keep.items():
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            (dst / rel).write_bytes(body)
        p = sync_tipo.plan(staged, dst, load_spec())
        listed = {f for k in ("add", "update", "remove", "same") for f in p[k]}
        for rel in keep:
            self.assertNotIn(rel, listed)
        sync_tipo.apply(staged, dst, p)
        for rel, body in keep.items():
            self.assertEqual((dst / rel).read_bytes(), body, rel)
        self.assertTrue((dst / "program" / "core" / "traffic_monitor.py").exists())

    def test_removes_stale_program_files_only(self):
        staged = self._staged()
        dst = self.tmp / "dst"
        for rel in ("web/js/old_tab.js", "data/notes.txt", "my_notes.txt"):
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            (dst / rel).write_text("x", encoding="utf-8")
        p = sync_tipo.plan(staged, dst, load_spec())
        self.assertIn("web/js/old_tab.js", p["remove"])
        self.assertNotIn("data/notes.txt", p["remove"])
        self.assertNotIn("my_notes.txt", p["remove"])
        sync_tipo.apply(staged, dst, p)
        self.assertFalse((dst / "web" / "js" / "old_tab.js").exists())
        self.assertTrue((dst / "data" / "notes.txt").exists())
        self.assertTrue((dst / "my_notes.txt").exists())


class StartupTest(SyncTestCase):
    """build 出來的版本實際啟動。埠 8799,逐分鐘明細導到暫存資料夾。"""

    def test_synced_copy_reads_tipo_state(self):
        out = self.tmp / "out"
        sync_tipo.build(REPO, load_spec(), out)
        result = sync_tipo.verify_startup(out, REPO, port=8799)
        self.assertTrue(result["ok"], result)
        self.assertGreaterEqual(result["used_bytes"], sync_tipo.PROBE_RECV + sync_tipo.PROBE_SENT)
        self.assertEqual(result["version"], sync_tipo.source_version(REPO))

    def test_synced_copy_starts_with_old_tipo_config(self):
        out = self.tmp / "out"
        sync_tipo.build(REPO, load_spec(), out)
        result = sync_tipo.verify_startup(out, REPO, port=8799, config=OLD_TIPO_CONFIG)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["config_status"], 200)
        self.assertEqual(result["config"]["quota_gb"], 2.0)


if __name__ == "__main__":
    unittest.main()
