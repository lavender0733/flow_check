"""Gmail REST v1 唯讀用戶端。

google 套件在建構子裡才 import:沒裝時只有 690 監控停用(ImportError 交給 service 處理),
流量監控照常啟動。token 被 refresh 後原子寫回,下次啟動不必重新授權。
"""

import base64
import json
import time
from pathlib import Path

from common.jsonio import write_json_atomic

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
API = "https://gmail.googleapis.com/gmail/v1/users/me"
TIMEOUT = 30
# 限流(429,或 403 帶這些 reason)不是授權問題:等一下重試,用完次數算一般抓信失敗
RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
RETRY_WAITS = (2, 5, 10)


class AuthError(Exception):
    """沒有 token、refresh 失敗、或 Gmail 回 401/403(授權失效或被管理員封鎖;限流除外)。"""


class GmailError(Exception):
    """其他 HTTP 或網路錯誤。"""


class GmailClient:
    def __init__(self, token_path, session=None, sleep=time.sleep):
        self._sleep = sleep
        token_path = Path(token_path)
        if not token_path.exists():
            raise AuthError(f"沒有授權檔 {token_path.name}")
        if session is not None:      # 測試注入
            self._s = session
            return
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import AuthorizedSession, Request
        from google.oauth2.credentials import Credentials
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            if not creds.valid:
                before = creds.token
                creds.refresh(Request())
                if creds.token != before:
                    write_json_atomic(token_path, json.loads(creds.to_json()))
        except (RefreshError, ValueError) as e:
            raise AuthError(str(e)) from e
        self._s = AuthorizedSession(creds)

    def _get(self, path: str, params: dict) -> dict:
        for wait in RETRY_WAITS + (None,):
            r = self._get_once(path, params)
            if not _rate_limited(r):
                break
            if wait is None:
                raise GmailError(f"Gmail 限流(HTTP {r.status_code}),重試後仍失敗,下一輪再抓")
            self._sleep(wait)
        if r.status_code in (401, 403):
            why = _reason(r)
            raise AuthError(f"Gmail 回 HTTP {r.status_code}" + (f"({why})" if why else ""))
        if r.status_code != 200:
            raise GmailError(f"Gmail 回 HTTP {r.status_code}:{r.text[:200]}")
        return r.json()

    def _get_once(self, path: str, params: dict):
        try:
            return self._s.get(f"{API}/{path}", params=params, timeout=TIMEOUT)
        except AuthError:
            raise
        except Exception as e:
            if type(e).__name__ == "RefreshError":
                raise AuthError(str(e)) from e
            raise GmailError(f"連不上 Gmail:{e}") from e

    def account(self) -> str:
        """授權的信箱帳號(信件與連線頁籤顯示用)。"""
        return self._get("profile", {}).get("emailAddress", "")

    def list_ids(self, query: str, limit: int = 1000) -> list:
        out, token = [], None
        while len(out) < limit:
            params = {"q": query, "maxResults": min(500, limit - len(out))}
            if token:
                params["pageToken"] = token
            r = self._get("messages", params)
            out += [m["id"] for m in r.get("messages", [])]
            token = r.get("nextPageToken")
            if not token:
                break
        return out

    def fetch_meta(self, msg_id: str) -> dict:
        r = self._get(f"messages/{msg_id}",
                      {"format": "metadata", "metadataHeaders": ["Subject", "Date"]})
        h = {x["name"].lower(): x["value"] for x in (r.get("payload") or {}).get("headers", [])}
        return {"subject": h.get("subject", ""), "date": h.get("date", ""),
                "size": int(r.get("sizeEstimate") or 0)}

    def fetch_raw(self, msg_id: str) -> bytes:
        r = self._get(f"messages/{msg_id}", {"format": "raw"})
        s = r.get("raw") or ""
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _errors(r) -> list:
    try:
        errs = (r.json().get("error") or {}).get("errors") or []
    except (ValueError, AttributeError):
        return []
    return [e for e in errs if isinstance(e, dict)]


def _reason(r) -> str:
    return ", ".join(str(e.get("reason")) for e in _errors(r) if e.get("reason"))


def _rate_limited(r) -> bool:
    if r.status_code == 429:
        return True
    return r.status_code == 403 and any(e.get("reason") in RATE_REASONS for e in _errors(r))
