"""690 資轉監控的組裝:設定、抓信、快取、狀態、wiki、背景迴圈。

流量預算(這套工具本身就在管每天 2 GB 的額度):一律先抓 metadata(主旨、時間,每封約 2 KB),
只有三種信才下載全文 —— 失敗信、期別系統每段的第一封(帶出 VOL)、要填筆數的步驟。

Wiki 只在使用者按「寫入 Wiki」時寫;JWT 只放記憶體,不寫檔、不進 snapshot 或快取。
"""

import copy
import re
import threading
from datetime import datetime, timedelta

from common.jsonio import read_json, write_json_atomic
from core.paths import (GMAIL_CREDENTIALS_PATH, GMAIL_TOKEN_PATH, XFER690_CACHE_PATH,
                        XFER690_CONFIG_PATH, XFER690_SECRETS_PATH)
from xfer690.parser import TPE, log_text, parse_raw, parse_subject
from xfer690.status import build_status, group_runs

# 預設值。systems / tables 留空:實際的系統清單在 data/config/xfer690.json(進版控)。
DEFAULT_XFER_CONFIG = {
    "enabled": True,
    "poll_interval_min": 10,
    "retain_days": 45,
    "history_days": 14,
    "project": "s690",
    "gmail_query": 'list:tipo.innovue.ltd subject:"[s690]"',
    "status_map": {"SUCCESS": "ok", "FAILURE": "fail"},
    "nodata_re": "GCS 資料夾無檔案",
    "max_raw_kb": 512,
    "systems": [],
    "wiki": {
        "enabled": False,
        "dry_run": True,
        "url": "https://km.innovue.ltd",
        "locale": "zh-tw",
        "days": 14,
        "placeholder_re": r"^\d{4}x{4}$",
        "base": "QC部/40_其他專案系統/智慧局-690整合式專利檢索系統/資料轉置登記",
        "tables": [],
    },
}

META_BYTES = 2048           # 一封 metadata 的流量估算
RECENT = 20
AUTH_MSG = "Gmail 授權已失效或被管理員封鎖,請雙擊 690授權.bat"
_MSG_ID_RE = re.compile(r"^[0-9a-fA-F]{6,32}$")


def load_xfer_config(path=XFER690_CONFIG_PATH) -> dict:
    """預設值合併檔案內容。wiki 子物件做一層合併;陣列直接取代。讀不到或壞檔回預設值。"""
    cfg = copy.deepcopy(DEFAULT_XFER_CONFIG)
    data = read_json(path, default={})
    if not isinstance(data, dict):
        return cfg
    for k, v in data.items():
        if k == "wiki" and isinstance(v, dict):
            cfg["wiki"].update(v)
        else:
            cfg[k] = v
    return cfg


class NotLoggedIn(Exception):
    pass


def _default_gmail():
    from xfer690.gmail_client import GmailClient
    return GmailClient(GMAIL_TOKEN_PATH)


def _default_wiki(url, token=None):
    from xfer690.wiki import WikiClient
    return WikiClient(url, token)


class Xfer690Service:
    def __init__(self, config_path=XFER690_CONFIG_PATH, cache_path=XFER690_CACHE_PATH,
                 secrets_path=XFER690_SECRETS_PATH, credentials_path=GMAIL_CREDENTIALS_PATH,
                 gmail_factory=_default_gmail, wiki_factory=_default_wiki,
                 now=lambda: datetime.now(TPE)):
        self.config_path = config_path
        self.cache_path = cache_path
        self.secrets_path = secrets_path
        self.credentials_path = credentials_path
        self.gmail_factory = gmail_factory
        self.wiki_factory = wiki_factory
        self.now = now
        self.cfg = load_xfer_config(config_path)
        self._lock = threading.Lock()         # 整個 poll()
        self._wiki_lock = threading.Lock()    # 登入 / 寫入 / 預覽
        self._stop = threading.Event()
        self._jwt = None                      # 只放記憶體
        self._contents = {}                   # 上次讀到的 wiki 頁面(預覽用,只放記憶體)

        cache = read_json(cache_path, default={})
        cache = cache if isinstance(cache, dict) else {}
        self.events = cache.get("events") if isinstance(cache.get("events"), dict) else {}
        self.last_poll = cache.get("last_poll")
        self.last_poll_kind = cache.get("last_poll_kind")
        self.last_error = None
        self.auth = "unknown"
        t = cache.get("traffic") or {}
        self.traffic = {"day": t.get("day"), "today_bytes": int(t.get("today_bytes") or 0),
                        "last_poll_bytes": int(t.get("last_poll_bytes") or 0)}
        w = cache.get("wiki") or {}
        self.wiki = {"last_sync": None, "last_error": None, "report": None, "tables": [],
                     "last_login": None, "last_write": w.get("last_write")}

    # ── 抓信 ────────────────────────────────────────────────
    def _add_bytes(self, n: int) -> None:
        day = self.now().date().isoformat()
        if self.traffic["day"] != day:
            self.traffic.update(day=day, today_bytes=0)
        self.traffic["today_bytes"] += n

    def _query(self) -> str:
        q = f"({self.cfg['gmail_query']})"
        if self.last_poll:
            epoch = int(datetime.fromisoformat(self.last_poll).timestamp()) - 3600
            return f"{q} after:{epoch}"
        return f"{q} newer_than:{int(self.cfg['retain_days'])}d"

    def _raw_wanted(self, new: dict) -> set:
        """這次新收到的信裡,要下載全文的 id。"""
        want = {i for i, e in new.items() if e["status"] == "fail"}
        writes = {c["step"] for t in self.cfg["wiki"].get("tables", [])
                  for c in (t.get("cols") or {}).values() if c.get("value") == "writes" and c.get("step")}
        want |= {i for i, e in new.items() if e["code"] in writes}
        for s in self.cfg["systems"]:
            if not str(s.get("period", "")).startswith("vol"):
                continue
            codes = set(s.get("auto", [])) | set(s.get("steps", []))
            evs = [e for e in list(self.events.values()) + list(new.values())
                   if e.get("code") in codes and e.get("at")]
            for seg in group_runs(evs, s):
                # 每段只要一封帶出 VOL:取第一封子步驟(母 job 的信排在最後,通常不是第一封)
                first = next((e for e in seg if e["code"] in s.get("steps", [])), seg[0])
                if any(e.get("raw") for e in seg if e["id"] not in new):
                    continue
                if first["id"] in new:
                    want.add(first["id"])
        return want

    def poll(self, kind: str = "auto") -> dict:
        with self._lock:
            self.cfg = load_xfer_config(self.config_path)
            if not self.cfg.get("enabled", True):
                return self.snapshot()
            spent = 0
            try:
                if not self.credentials_path.exists():
                    self.auth = "missing_credentials"
                    self.last_error = f"找不到 {self.credentials_path.name},請照 690授權.bat 的說明設定"
                    return self.snapshot()
                try:
                    gmail = self.gmail_factory()
                except ImportError:
                    self.auth = "missing_package"
                    self.last_error = "缺少套件 google-auth-oauthlib,請執行 python -m pip install -r requirements-690.txt"
                    return self.snapshot()
                ids = gmail.list_ids(self._query())
                new = {}
                for mid in ids:
                    if mid in self.events:
                        continue
                    meta = gmail.fetch_meta(mid)
                    spent += META_BYTES
                    ev = parse_subject(meta["subject"], meta["date"], mid, self.cfg)
                    ev["size"] = meta["size"]
                    new[mid] = ev
                limit = int(self.cfg["max_raw_kb"]) * 1024
                for mid in sorted(self._raw_wanted(new)):
                    ev = new[mid]
                    if not ev.get("code") or ev["size"] > limit:
                        continue
                    raw = gmail.fetch_raw(mid)
                    spent += ev["size"] or len(raw)
                    try:
                        new[mid] = parse_raw(raw, ev, self.cfg)
                    except Exception as e:      # 解析不了的信照樣留下 metadata
                        ev["error"] = f"解析失敗:{e}"[:200]
                self.events.update(new)
                self._prune()
                self.last_poll = self.now().isoformat()
                self.last_poll_kind = kind
                self.last_error = None
                self.auth = "ok"
            except Exception as e:
                if type(e).__name__ == "AuthError":
                    self.auth = "needs_auth"
                    self.last_error = AUTH_MSG
                else:
                    self.last_error = f"抓信失敗:{e}"[:300]
            finally:
                self._add_bytes(spent)
                self.traffic["last_poll_bytes"] = spent
                self._save()
        if self._jwt:
            self._refresh_preview()
        return self.snapshot()

    def _prune(self) -> None:
        cut = self.now() - timedelta(days=int(self.cfg["retain_days"]))
        for mid in [m for m, e in self.events.items()
                    if e.get("at") and datetime.fromisoformat(e["at"]) < cut]:
            del self.events[mid]

    def _save(self) -> None:
        try:
            write_json_atomic(self.cache_path, {
                "events": self.events, "last_poll": self.last_poll,
                "last_poll_kind": self.last_poll_kind, "traffic": self.traffic,
                "wiki": {"last_write": self.wiki["last_write"]}})
        except OSError as e:
            self.last_error = f"快取寫不進去:{e}"

    # ── 畫面 ────────────────────────────────────────────────
    def status(self) -> dict:
        return build_status(list(self.events.values()), self.cfg["systems"], self.now(),
                            int(self.cfg["history_days"]))

    def snapshot(self) -> dict:
        now = self.now()
        stale = True
        if self.last_poll:
            age = now - datetime.fromisoformat(self.last_poll)
            stale = age > timedelta(minutes=3 * int(self.cfg["poll_interval_min"]))
        recent = sorted((e for e in self.events.values() if e.get("code") and e.get("at")),
                        key=lambda e: e["at"], reverse=True)[:RECENT]
        w = self.cfg["wiki"]
        traffic = dict(self.traffic)
        if traffic.get("day") != now.date().isoformat():
            traffic["today_bytes"] = 0
        return {
            "ok": True, "enabled": bool(self.cfg.get("enabled", True)),
            "auth": self.auth, "last_poll": self.last_poll, "last_poll_kind": self.last_poll_kind,
            "last_error": self.last_error, "stale": stale,
            "poll_interval_min": int(self.cfg["poll_interval_min"]),
            "traffic": {"today_bytes": traffic["today_bytes"],
                        "last_poll_bytes": traffic["last_poll_bytes"]},
            "status": self.status(),
            "recent": recent,
            "wiki": {"enabled": bool(w.get("enabled")), "dry_run": bool(w.get("dry_run", True)),
                     "url": w.get("url"), "logged_in": self._jwt is not None,
                     "last_login": self.wiki["last_login"], "last_write": self.wiki["last_write"],
                     "last_sync": self.wiki["last_sync"], "last_error": self.wiki["last_error"],
                     "report": self.wiki["report"], "tables": self.wiki["tables"]},
        }

    def log(self, msg_id: str) -> str:
        """那封信附的 .log 全文。當下抓、不存檔;只限快取裡有的信。"""
        if not msg_id or not _MSG_ID_RE.match(msg_id) or msg_id not in self.events:
            raise KeyError(msg_id)
        raw = self.gmail_factory().fetch_raw(msg_id)
        self._add_bytes(len(raw))
        return log_text(raw)

    def loop(self) -> None:
        from core.health import record_error
        while not self._stop.is_set():
            try:
                self.poll("auto")
            except Exception as e:
                record_error("xfer690", e)
            self._stop.wait(int(self.cfg.get("poll_interval_min", 10)) * 60)

    def stop(self) -> None:
        self._stop.set()

    # ── Wiki ────────────────────────────────────────────────
    def wiki_login(self) -> dict:
        """讀 xfer690_secrets.json 登入,JWT 留在記憶體。回應只寫原因,不回顯帳號密碼。"""
        from xfer690.wiki import WikiError
        w = self.cfg["wiki"]
        name = self.secrets_path.name
        with self._wiki_lock:
            if not w.get("enabled"):
                return self._login_result(False, "Wiki 功能還沒啟用(xfer690.json 的 wiki.enabled)")
            secrets = read_json(self.secrets_path, default=None)
            if not isinstance(secrets, dict):
                return self._login_result(False, f"找不到或讀不了 data/config/{name}")
            missing = [k for k in ("wiki_user", "wiki_password") if not secrets.get(k)]
            if missing:
                return self._login_result(False, f"{name} 缺少欄位:{'、'.join(missing)}")
            try:
                client = self.wiki_factory(w["url"])
                self._jwt = client.login(secrets["wiki_user"], secrets["wiki_password"],
                                         secrets.get("wiki_strategy") or "local")
            except WikiError:
                self._jwt = None
                return self._login_result(False, f"登入失敗,請檢查 {name} 的 wiki_user、wiki_password")
            except Exception as e:
                self._jwt = None
                return self._login_result(False, f"連不上 Wiki:{type(e).__name__}")
            res = self._login_result(True, "")
        self._refresh_preview()
        return res

    def _login_result(self, ok: bool, message: str) -> dict:
        self.wiki["last_login"] = {"at": self.now().isoformat(), "ok": ok, "message": message}
        return {"ok": ok, "message": message}

    def _expire(self, message: str) -> None:
        self._jwt = None
        self.wiki["last_login"] = {"at": self.now().isoformat(), "ok": False, "message": message}

    def _refresh_preview(self) -> None:
        """登入狀態下讀頁面、算預覽(不寫)。任何錯誤只記在 wiki.last_error。"""
        from xfer690.wiki import WikiAuthError, sync
        with self._wiki_lock:
            if not self._jwt:
                return
            try:
                client = self.wiki_factory(self.cfg["wiki"]["url"], self._jwt)
                res = sync(client, self.status(), self.cfg["wiki"], write=False)
                self._contents = res["contents"]
                self.wiki.update(report=res["pages"], tables=res["tables"],
                                 last_sync=self.now().isoformat(), last_error=None)
            except WikiAuthError:
                self._expire("登入已失效,請重新登入")
            except Exception as e:
                self.wiki["last_error"] = f"讀 Wiki 失敗:{e}"[:300]

    def wiki_write(self, edits=()) -> dict:
        """用記憶體裡的 JWT 寫入(使用者按「寫入 Wiki」)。沒有 JWT → NotLoggedIn。"""
        from xfer690.wiki import WikiAuthError, sync
        edits = [e for e in (edits or []) if isinstance(e, dict)]
        with self._wiki_lock:
            if not self._jwt:
                raise NotLoggedIn()
            try:
                client = self.wiki_factory(self.cfg["wiki"]["url"], self._jwt)
                res = sync(client, self.status(), self.cfg["wiki"], edits=edits, write=True)
            except WikiAuthError:
                self._expire("登入已失效,請重新登入")
                raise NotLoggedIn()
            except Exception as e:
                self.wiki["last_error"] = f"寫 Wiki 失敗:{e}"[:300]
                return {"ok": False, "message": self.wiki["last_error"]}
            pages = res["pages"]
            self._contents = res["contents"]
            self.wiki.update(report=pages, tables=res["tables"], last_sync=self.now().isoformat(),
                             last_error=None)
            self.wiki["last_write"] = {
                "at": self.now().isoformat(),
                "pages": sum(1 for p in pages.values() if p.get("updated")),
                "filled": sum(p.get("filled", 0) for p in pages.values()),
                "inserted": sum(p.get("inserted", 0) for p in pages.values()),
                "edited": sum(p.get("edited", 0) for p in pages.values()),
                "dry_run": bool(self.cfg["wiki"].get("dry_run", True))}
            self._save()
            return {"ok": True, "dry_run": bool(self.cfg["wiki"].get("dry_run", True)),
                    "pages": pages, "last_write": self.wiki["last_write"]}
