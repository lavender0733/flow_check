"""Gmail REST v1 唯讀用戶端。

google 套件在建構子裡才 import:沒裝時只有 690 監控停用(ImportError 交給 service 處理),
流量監控照常啟動。token 被 refresh 後原子寫回,下次啟動不必重新授權。
"""

import base64
import json
from pathlib import Path

from common.jsonio import write_json_atomic

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
API = "https://gmail.googleapis.com/gmail/v1/users/me"
TIMEOUT = 30


class AuthError(Exception):
    """沒有 token、refresh 失敗、或 Gmail 回 401/403(授權失效或被管理員封鎖)。"""


class GmailError(Exception):
    """其他 HTTP 或網路錯誤。"""


class GmailClient:
    def __init__(self, token_path, session=None):
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
        try:
            r = self._s.get(f"{API}/{path}", params=params, timeout=TIMEOUT)
        except AuthError:
            raise
        except Exception as e:
            if type(e).__name__ == "RefreshError":
                raise AuthError(str(e)) from e
            raise GmailError(f"連不上 Gmail:{e}") from e
        if r.status_code in (401, 403):
            raise AuthError(f"Gmail 回 HTTP {r.status_code}")
        if r.status_code != 200:
            raise GmailError(f"Gmail 回 HTTP {r.status_code}:{r.text[:200]}")
        return r.json()

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
