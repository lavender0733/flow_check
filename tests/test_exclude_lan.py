"""排除內網流量一律開啟:沒有開關,舊的 prefs(exclude_lan=false)也不能把它關掉。"""

import unittest
from unittest import mock

import _util  # noqa: F401  (sys.path)
import core.routes as routes
import core.sampler as sampler
from core.storage import DEFAULT_CONFIG


class ExcludeLanAlwaysOnTest(unittest.TestCase):
    def test_monitor_ignores_old_pref_off(self):
        # 以前在頁面關過的機器,prefs 裡留著 exclude_lan=false
        with mock.patch.object(sampler, "load_prefs", return_value={"exclude_lan": False}):
            m = sampler.Monitor(dict(DEFAULT_CONFIG), counters_fn=lambda: {})
        self.assertTrue(m.exclude_lan_enabled)

    def test_monitor_ignores_config_off(self):
        cfg = dict(DEFAULT_CONFIG, exclude_lan=False)
        with mock.patch.object(sampler, "load_prefs", return_value={}):
            m = sampler.Monitor(cfg, counters_fn=lambda: {})
        self.assertTrue(m.exclude_lan_enabled)

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
