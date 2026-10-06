import base64
import json
import unittest

from _util import TempDataCase
from xfer690.gmail_client import AuthError, GmailClient, GmailError


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return self.responses.pop(0)


class GmailTest(TempDataCase):
    def client(self, responses):
        tok = self.write_json("token.json", {"token": "x"})
        sess = FakeSession(responses)
        self.slept = []
        return GmailClient(tok, session=sess, sleep=self.slept.append), sess

    def test_missing_token_raises_auth_error(self):
        with self.assertRaises(AuthError):
            GmailClient(self.tmp / "nope.json")

    def test_fetch_raw_decodes_base64url(self):
        raw = "Subject: x\r\n\r\nhi ~?>".encode("utf-8")
        enc = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        c, sess = self.client([FakeResp(body={"raw": enc})])
        self.assertEqual(c.fetch_raw("m1"), raw)
        self.assertEqual(sess.calls[0][1], {"format": "raw"})

    def test_list_ids_follows_pages(self):
        c, sess = self.client([
            FakeResp(body={"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "p2"}),
            FakeResp(body={"messages": [{"id": "c"}]}),
        ])
        self.assertEqual(c.list_ids("q"), ["a", "b", "c"])
        self.assertEqual(sess.calls[1][1]["pageToken"], "p2")
        self.assertEqual(sess.calls[0][1]["q"], "q")

    def test_list_ids_empty(self):
        c, _ = self.client([FakeResp(body={"resultSizeEstimate": 0})])
        self.assertEqual(c.list_ids("q"), [])

    def test_fetch_meta_shape(self):
        c, sess = self.client([FakeResp(body={"sizeEstimate": 70000, "payload": {"headers": [
            {"name": "Subject", "value": "🟢【SUCCESS】[s690] 012001-a | g"},
            {"name": "Date", "value": "Thu, 01 Oct 2026 02:10:02 +0800"}]}})])
        self.assertEqual(c.fetch_meta("m1"), {"subject": "🟢【SUCCESS】[s690] 012001-a | g",
                                              "date": "Thu, 01 Oct 2026 02:10:02 +0800", "size": 70000})
        self.assertEqual(sess.calls[0][1]["format"], "metadata")
        self.assertEqual(sess.calls[0][1]["metadataHeaders"], ["Subject", "Date"])

    def test_401_maps_to_auth_error(self):
        for code in (401, 403):
            c, _ = self.client([FakeResp(code, {"error": {"message": "nope"}})])
            with self.assertRaises(AuthError):
                c.list_ids("q")

    def test_rate_limit_retried(self):
        # 逐封讀主旨時 Gmail 會回 403 rateLimitExceeded:等一下重試,不是授權失效
        for status, reason in ((403, "rateLimitExceeded"), (403, "userRateLimitExceeded"), (429, "")):
            body = {"error": {"code": status, "message": "slow down",
                              "errors": [{"reason": reason}] if reason else []}}
            c, sess = self.client([FakeResp(status, body), FakeResp(status, body),
                                   FakeResp(body={"messages": [{"id": "a"}]})])
            self.assertEqual(c.list_ids("q"), ["a"], reason or status)
            self.assertEqual(len(sess.calls), 3)
            self.assertEqual(len(self.slept), 2)
            self.assertLess(self.slept[0], self.slept[1])

    def test_auth_error_names_reason(self):
        body = {"error": {"code": 403, "errors": [{"reason": "insufficientPermissions"}]}}
        c, _ = self.client([FakeResp(403, body)])
        with self.assertRaises(AuthError) as cm:
            c.list_ids("q")
        self.assertIn("insufficientPermissions", str(cm.exception))
        self.assertEqual(self.slept, [])

    def test_rate_limit_exhausted_is_gmail_error(self):
        body = {"error": {"code": 403, "errors": [{"reason": "rateLimitExceeded"}]}}
        c, sess = self.client([FakeResp(403, body) for _ in range(10)])
        with self.assertRaises(GmailError) as cm:
            c.fetch_meta("m")
        self.assertNotIsInstance(cm.exception, AuthError)
        self.assertIn("限流", str(cm.exception))
        self.assertEqual(len(sess.calls), 4)

    def test_500_maps_to_gmail_error(self):
        c, _ = self.client([FakeResp(500, {"error": {"message": "boom"}})])
        with self.assertRaises(GmailError):
            c.fetch_meta("m")


if __name__ == "__main__":
    unittest.main()
