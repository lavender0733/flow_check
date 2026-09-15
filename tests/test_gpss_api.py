"""GPSS API 檢索的純解析函式（完全不碰網路）。"""

from unittest.mock import patch

from _util import TempDataCase

from gpss import gpss_api_query as g


class TestNormPn(TempDataCase):
    def test_strips_punctuation_and_uppercases(self):
        self.assertEqual(g._norm_pn("tw-i123,456 b"), "TWI123456B")

    def test_empty(self):
        self.assertEqual(g._norm_pn(""), "")
        self.assertEqual(g._norm_pn(None), "")


class TestParseRecords(TempDataCase):
    def _wrap(self, contents):
        return {"gpss-API": {"patent": {"patentcontent": contents}}}

    def test_single_content_dict_is_wrapped(self):
        data = self._wrap({
            "publication-reference": {"doc-number": "I123456"},
            "application-reference": {"doc-number": "105123456"},
        })
        self.assertEqual(g.parse_records(data), [("I123456", "105123456")])

    def test_list_of_contents(self):
        data = self._wrap([
            {"publication-reference": {"doc-number": "A1"},
             "application-reference": {"doc-number": "B1"}},
            {"publication-reference": {"doc-number": "A2"},
             "application-reference": {"doc-number": "B2"}},
        ])
        self.assertEqual(g.parse_records(data), [("A1", "B1"), ("A2", "B2")])

    def test_missing_references_become_empty_strings(self):
        self.assertEqual(g.parse_records(self._wrap([{}])), [("", "")])

    def test_malformed_shapes_return_empty(self):
        self.assertEqual(g.parse_records({}), [])
        self.assertEqual(g.parse_records(None), [])
        self.assertEqual(g.parse_records({"gpss-API": None}), [])
        self.assertEqual(g.parse_records({"gpss-API": {"patent": None}}), [])
        self.assertEqual(g.parse_records(self._wrap("not a list")), [])

    def test_non_dict_entries_skipped(self):
        data = self._wrap([
            "junk",
            {"publication-reference": {"doc-number": "A1"},
             "application-reference": {"doc-number": "B1"}},
        ])
        self.assertEqual(g.parse_records(data), [("A1", "B1")])


class TestIsNodeFailure(TempDataCase):
    def test_node_and_non_json(self):
        self.assertTrue(g.is_node_failure("NODE_ERR"))
        self.assertTrue(g.is_node_failure("NON_JSON"))

    def test_network_and_exception_prefixes(self):
        self.assertTrue(g.is_node_failure("NET_ERROR: timed out"))
        self.assertTrue(g.is_node_failure("EXCEPTION: boom"))

    def test_http_5xx_and_429_are_node_failures(self):
        self.assertTrue(g.is_node_failure("HTTP_500"))
        self.assertTrue(g.is_node_failure("HTTP_503: unavailable"))
        self.assertTrue(g.is_node_failure("HTTP_429"))

    def test_http_4xx_other_than_429_is_not(self):
        self.assertFalse(g.is_node_failure("HTTP_403"))
        self.assertFalse(g.is_node_failure("HTTP_404"))

    def test_unparsable_http_code_treated_as_node_failure(self):
        self.assertTrue(g.is_node_failure("HTTP_abc"))

    def test_plain_fail_is_not_a_node_failure(self):
        self.assertFalse(g.is_node_failure("fail"))
        self.assertFalse(g.is_node_failure("OK"))


class TestRequestDelay(TempDataCase):
    BASE_CFG = {
        "busy_start_hour": 8,
        "busy_end_hour": 18,
        "quota_per_hour_busy": 3600,
        "quota_per_hour_offpeak": 7200,
        "quota_safety": 1.0,
        "min_request_delay": 2,
        "quota_counts": "pn",
    }

    def test_counts_by_patent_number(self):
        """額度以專利筆數計：batch_len / (budget/3600)。
        budget=3600 -> 每秒 1 筆 -> 一批 10 筆需 10 秒。"""
        with patch.object(g, "is_busy_hours", return_value=True):
            self.assertAlmostEqual(g.request_delay(self.BASE_CFG, 10), 10.0)

    def test_counts_by_request(self):
        """額度以請求數計：3600/budget，與批次大小無關。"""
        cfg = dict(self.BASE_CFG, quota_counts="request", quota_per_hour_busy=360)
        with patch.object(g, "is_busy_hours", return_value=True):
            self.assertAlmostEqual(g.request_delay(cfg, 10), 10.0)

    def test_offpeak_budget_used_when_not_busy(self):
        """下班時段額度較大 -> 延遲較短。budget=7200 -> 每秒 2 筆 -> 10 筆需 5 秒。"""
        with patch.object(g, "is_busy_hours", return_value=False):
            self.assertAlmostEqual(g.request_delay(self.BASE_CFG, 10), 5.0)

    def test_safety_factor_shrinks_budget(self):
        """safety=0.5 讓可用額度減半 -> 延遲加倍。"""
        cfg = dict(self.BASE_CFG, quota_safety=0.5)
        with patch.object(g, "is_busy_hours", return_value=True):
            self.assertAlmostEqual(g.request_delay(cfg, 10), 20.0)

    def test_never_below_minimum(self):
        cfg = dict(self.BASE_CFG, quota_per_hour_busy=3600000)
        with patch.object(g, "is_busy_hours", return_value=True):
            self.assertEqual(g.request_delay(cfg, 1), 2.0)

    def test_zero_budget_falls_back_to_minimum(self):
        cfg = dict(self.BASE_CFG, quota_per_hour_busy=0)
        with patch.object(g, "is_busy_hours", return_value=True):
            self.assertEqual(g.request_delay(cfg, 10), 2.0)


class TestChunked(TempDataCase):
    def test_even_split(self):
        self.assertEqual(list(g.chunked([1, 2, 3, 4], 2)), [[1, 2], [3, 4]])

    def test_remainder_kept(self):
        self.assertEqual(list(g.chunked([1, 2, 3], 2)), [[1, 2], [3]])

    def test_size_larger_than_sequence(self):
        self.assertEqual(list(g.chunked([1, 2], 10)), [[1, 2]])

    def test_empty(self):
        self.assertEqual(list(g.chunked([], 3)), [])


class TestMaskCode(TempDataCase):
    def test_masks_after_four(self):
        self.assertEqual(g._mask_code("21dcBD8B96d02A77"), "21dc" + "*" * 12)

    def test_short_unchanged(self):
        self.assertEqual(g._mask_code("abcd"), "abcd")

    def test_does_not_strip_whitespace_yet(self):
        """現行行為：gpss 版沒有 .strip()，與另外兩份不一致。

        Task 12 把呼叫點換成 common.ident.mask_code（有 strip）後，
        這個測試會被改成
        assertEqual(..., "abcd**")。這是刻意留下的路標，不是 bug。
        """
        self.assertEqual(g._mask_code("  abcdef  "), "  ab" + "*" * 6)
