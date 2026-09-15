"""流量計費核心邏輯。

Monitor 的設計前提寫在 traffic_monitor.py 的 docstring：讀網卡「累積」
計數器而非取樣速率，所以 current - last 永遠精確、取樣間隔再長也不漏算；
重開機會讓計數器歸零，用 current < last 偵測並把當前值當成重開後的增量。

這段邏輯從未被驗證過。以下測試把它鎖住。
"""

from collections import namedtuple
from unittest.mock import patch

from _util import TempDataCase

from core import traffic_monitor as tm


# 形狀與 psutil.net_io_counters(pernic=True) 的值相同（只取用得到的兩欄）
Counter = namedtuple("Counter", "bytes_sent bytes_recv")


CFG = {
    "quota_gb": 2.0,
    "sample_interval_sec": 60,
    "exclude_nics": ["Loopback", "isatap", "Teredo"],
    "exclude_lan": False,
    "lan_ranges": [],
    "count_ranges": [],
    "surge_enabled": False,
    "peak_window_min": 10,
    "surge_min_mb": 50,
    "surge_factor": 4.0,
    "surge_window_min": 30,
    "surge_cooldown_sec": 300,
}


class MonitorCase(TempDataCase):
    """建立一個完全隔離的 Monitor：假計數器、暫存存檔路徑、不碰 ESTATS。"""

    def make_monitor(self, counters):
        """counters 是一個 dict，測試可以直接改它來模擬下一次取樣的讀數。"""
        self.counters = counters
        state = self.tmp / "state_TEST.json"
        with patch.object(tm, "STATE_PATH", state), \
             patch.object(tm, "DATA_DIR", self.tmp), \
             patch.object(tm, "load_prefs", return_value={}), \
             patch.object(tm, "process_is_elevated", return_value=False):
            mon = tm.Monitor(CFG, counters_fn=lambda: dict(self.counters))
        # 後續 sample() 會寫存檔，導到暫存路徑
        self.state_path = state
        self._state_patch = patch.object(tm, "STATE_PATH", state)
        self._state_patch.start()
        self.addCleanup(self._state_patch.stop)
        # 關掉暴增偵測：那條路徑會去掃 psutil 的行程清單，與本測試無關
        mon.surge_enabled = False
        return mon


class TestBaseline(MonitorCase):
    def test_first_sample_counts_nothing(self):
        """啟動時先把現況設為基準，不把開機以來的歷史累積算進今天。"""
        mon = self.make_monitor({"eth0": Counter(1_000_000, 5_000_000)})
        mon.sample()
        self.assertEqual(mon.day_sent, 0)
        self.assertEqual(mon.day_recv, 0)

    def test_delta_is_counted(self):
        mon = self.make_monitor({"eth0": Counter(1000, 5000)})
        mon.sample()
        self.counters["eth0"] = Counter(1500, 7000)
        mon.sample()
        self.assertEqual(mon.day_sent, 500)
        self.assertEqual(mon.day_recv, 2000)

    def test_large_gap_between_samples_is_still_exact(self):
        """累積計數器的重點：中間隔多久都不漏算。"""
        mon = self.make_monitor({"eth0": Counter(0, 0)})
        mon.sample()
        self.counters["eth0"] = Counter(10_000_000, 50_000_000)
        mon.sample()
        self.assertEqual(mon.day_sent, 10_000_000)
        self.assertEqual(mon.day_recv, 50_000_000)


class TestCounterReset(MonitorCase):
    def test_reboot_reset_uses_current_value_as_delta(self):
        """重開機後計數器歸零：current < last 時，把 current 當成重開後的增量，
        而不是算出一個負數（那會把當日用量往回扣）。"""
        mon = self.make_monitor({"eth0": Counter(9_000_000, 9_000_000)})
        mon.sample()
        self.counters["eth0"] = Counter(1200, 3400)     # 重開機，計數器歸零後又跑了一點
        mon.sample()
        self.assertEqual(mon.day_sent, 1200)
        self.assertEqual(mon.day_recv, 3400)

    def test_reset_never_produces_negative_usage(self):
        mon = self.make_monitor({"eth0": Counter(5_000_000, 5_000_000)})
        mon.sample()
        self.counters["eth0"] = Counter(0, 0)
        mon.sample()
        self.assertGreaterEqual(mon.day_sent, 0)
        self.assertGreaterEqual(mon.day_recv, 0)

    def test_only_the_reset_direction_is_rebased(self):
        """送方向回捲、收方向正常時，收方向仍照常算差值。"""
        mon = self.make_monitor({"eth0": Counter(9_000_000, 1000)})
        mon.sample()
        self.counters["eth0"] = Counter(50, 3000)
        mon.sample()
        self.assertEqual(mon.day_sent, 50)      # 回捲 -> 取當前值
        self.assertEqual(mon.day_recv, 2000)    # 正常 -> 取差值


class TestDayRollover(MonitorCase):
    def test_rollover_zeroes_the_day(self):
        """跨日把當日累計歸零，但 last_per_nic 保持連續，
        免得跨日那一刻倒出一個巨大的差值。"""
        mon = self.make_monitor({"eth0": Counter(0, 0)})
        mon.sample()
        self.counters["eth0"] = Counter(1000, 2000)
        mon.sample()
        self.assertEqual(mon.day_sent, 1000)

        # 假裝現在記的是昨天
        mon.date = "2020-01-01"
        self.counters["eth0"] = Counter(1100, 2200)
        mon.sample()

        # 歸零後只算跨日之後的那 100 / 200，不是累積的 1100 / 2200
        self.assertEqual(mon.day_sent, 100)
        self.assertEqual(mon.day_recv, 200)

    def test_rollover_clears_surges_and_lan_total(self):
        mon = self.make_monitor({"eth0": Counter(0, 0)})
        mon.sample()
        mon.day_lan_excluded = 12345
        mon.surges.append({"seq": 1})
        mon.date = "2020-01-01"
        mon.sample()
        self.assertEqual(mon.day_lan_excluded, 0)
        self.assertEqual(len(mon.surges), 0)


class TestNicExclusion(MonitorCase):
    def test_excluded_nics_are_not_counted(self):
        mon = self.make_monitor({
            "eth0": Counter(0, 0),
            "Loopback Pseudo-Interface 1": Counter(0, 0),
        })
        mon.sample()
        self.counters["eth0"] = Counter(100, 200)
        self.counters["Loopback Pseudo-Interface 1"] = Counter(9_999_999, 9_999_999)
        mon.sample()
        self.assertEqual(mon.day_sent, 100)
        self.assertEqual(mon.day_recv, 200)

    def test_exclusion_is_case_insensitive(self):
        mon = self.make_monitor({"eth0": Counter(0, 0), "ISATAP.{abc}": Counter(0, 0)})
        mon.sample()
        self.counters["eth0"] = Counter(10, 10)
        self.counters["ISATAP.{abc}"] = Counter(5000, 5000)
        mon.sample()
        self.assertEqual(mon.day_sent, 10)

    def test_nic_appearing_midrun_is_baselined_not_counted(self):
        """跑到一半才出現的網卡（插上 USB 網卡、VPN 上線）要先設基準，
        不能把它開機以來的累積量一次算進今天。"""
        mon = self.make_monitor({"eth0": Counter(0, 0)})
        mon.sample()
        self.counters["wlan0"] = Counter(8_000_000, 8_000_000)
        mon.sample()
        self.assertEqual(mon.day_sent, 0)
        self.assertEqual(mon.day_recv, 0)
        # 下一輪才開始算它的增量
        self.counters["wlan0"] = Counter(8_000_500, 8_000_700)
        mon.sample()
        self.assertEqual(mon.day_sent, 500)
        self.assertEqual(mon.day_recv, 700)


class TestStatePersistence(MonitorCase):
    def test_sample_writes_state_atomically(self):
        mon = self.make_monitor({"eth0": Counter(0, 0)})
        mon.sample()
        self.assertTrue(self.state_path.exists())
        # 原子寫入的暫存檔不該留下
        self.assertFalse(self.state_path.with_suffix(".tmp").exists())
