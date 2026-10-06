"""排除內網流量一律開啟:沒有開關,舊的 prefs(exclude_lan=false)也不能把它關掉,
而且扣除功能本身照常運作。所有檔案都在暫存目錄,不碰真的 data/。"""

import unittest
from collections import namedtuple
from unittest import mock

from _util import TempDataCase
import core.routes as routes
import core.sampler as sampler
from core.storage import DEFAULT_CONFIG

Nic = namedtuple("Nic", "bytes_sent bytes_recv")


class ExcludeLanAlwaysOnTest(TempDataCase):
    def setUp(self):
        super().setUp()
        self.counters = {"Ethernet": Nic(0, 0)}
        for name, path in (("STATE_PATH", self.tmp / "state.json"), ("DATA_DIR", self.tmp),
                           ("TRAFFIC_LOG_DIR", self.tmp / "logs")):
            p = mock.patch.object(sampler, name, path)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(sampler.daily_stats, "record_day", create=True)
        p.start()
        self.addCleanup(p.stop)

    def monitor(self, prefs=None, **cfg):
        with mock.patch.object(sampler, "load_prefs", return_value=prefs or {}):
            return sampler.Monitor(dict(DEFAULT_CONFIG, **cfg), counters_fn=lambda: self.counters)

    def test_monitor_ignores_old_pref_off(self):
        # 以前在頁面關過的機器,prefs 裡留著 exclude_lan=false
        self.assertTrue(self.monitor({"exclude_lan": False}).exclude_lan_enabled)

    def test_monitor_ignores_config_off(self):
        self.assertTrue(self.monitor(exclude_lan=False).exclude_lan_enabled)

    def test_lan_share_is_deducted(self):
        # 功能本身要還在:這分鐘 NIC 收 1000,其中內網 payload 占 60% → 只計 400、排除 600
        m = self.monitor({"exclude_lan": False})
        m.sample()                                  # 建立基準
        base_recv, base_lan = m.day_recv, m.day_lan_excluded
        self.counters = {"Ethernet": Nic(0, 1000)}
        m._pending_lan_recv, m._pending_ext_recv = 600, 400
        m.sample()
        self.assertEqual(m.day_recv - base_recv, 400)
        self.assertEqual(m.day_lan_excluded - base_lan, 600)

    def test_prefs_endpoint_does_not_toggle(self):
        saved = []

        class FakeMonitor:
            exclude_lan_enabled = True

        app = routes.App({}, FakeMonitor())
        with mock.patch.object(routes, "save_prefs", side_effect=lambda p: saved.append(p) or p), \
             mock.patch.object(routes, "load_prefs", return_value={}):
            routes.prefs_write(app, {"exclude_lan": False})
        self.assertTrue(app.monitor.exclude_lan_enabled)
        self.assertFalse(any("exclude_lan" in p for p in saved))

    def test_config_payload_has_no_toggle(self):
        class FakeMonitor:
            pass

        app = routes.App({"quota_gb": 1}, FakeMonitor())
        with mock.patch.object(routes, "load_prefs", return_value={"exclude_lan": False}):
            payload = routes.config_payload(app)
        self.assertNotIn("exclude_lan", payload)


if __name__ == "__main__":
    unittest.main()
