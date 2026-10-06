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
    "max_list": 5000,       # 一次最多列幾封(45 天約 740 封)
    "systems": [],
    "wiki": {
        "enabled": False,
        "dry_run": True,
        "url": "https://km.innovue.ltd",
        "locale": "zh-tw",
        "days": 14,
        "placeholder_re": r"^\d{4}x{4}$",
        "nodata_value": "-",        # 每日系統「GCS 資料夾無檔案」那天,要打 v 的格改填這個
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


EXPIRED_MSG = "Wiki 登入已失效,請重新用 Innovue OAuth 登入 Wiki 後再貼一次 jwt"
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")


def _all_pages_failed(pages: dict):
    """每一頁都讀失敗(例如被 Cloudflare 擋)時,回傳要顯示的錯誤;否則 None。

    sync 把每頁的錯誤收在 report 裡、不往外拋,全部失敗時畫面上要看得到原因。"""
    errs = [p["errors"][0] for p in pages.values() if p.get("errors")]
    if pages and len(errs) == len(pages):
        return f"讀 Wiki 失敗:{errs[0]}"[:300]
    return None


def _clean_jwt(pasted):
    """接受「值」本身,或連同 jwt= / Bearer 一起複製的;形狀不像 JWT 回 None(不連網)。"""
    if not isinstance(pasted, str):
        return None
    t = pasted.strip().strip('"').strip()
    for prefix in ("jwt=", "Bearer "):
        if t[:len(prefix)].lower() == prefix.lower():
            t = t[len(prefix):].strip()
    t = t.rstrip(";").strip()
    return t if len(t) <= 8192 and _JWT_RE.match(t) else None


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
        self._jwt = None                      # Wiki 登入憑證;也存在 secrets 檔(重開不用重貼)
        self._contents = {}                   # 上次讀到的 wiki 頁面(預覽用,只放記憶體)

        cache = read_json(cache_path, default={})
        cache = cache if isinstance(cache, dict) else {}
        self.events = cache.get("events") if isinstance(cache.get("events"), dict) else {}
        self.last_poll = cache.get("last_poll")
        self.last_poll_kind = cache.get("last_poll_kind")
        self.last_error = None
        self.auth = "unknown"
        self.account = cache.get("account")
        t = cache.get("traffic") or {}
        self.traffic = {"day": t.get("day"), "today_bytes": int(t.get("today_bytes") or 0),
                        "last_poll_bytes": int(t.get("last_poll_bytes") or 0)}
        w = cache.get("wiki") or {}
        self.wiki = {"last_sync": None, "last_error": None, "report": None, "tables": [],
                     "last_login": w.get("last_login"), "last_write": w.get("last_write"),
                     "account": w.get("account")}
        secrets = read_json(secrets_path, default={})
        jwt = secrets.get("wiki_jwt") if isinstance(secrets, dict) else None
        if isinstance(jwt, str) and _JWT_RE.match(jwt):
            self._jwt = jwt     # 上次貼的;失效的話第一次讀 wiki 時清掉

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
                max_list = int(self.cfg["max_list"])
                ids = gmail.list_ids(self._query(), max_list)
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
                if not self.account and hasattr(gmail, "account"):
                    try:
                        self.account = gmail.account()
                    except Exception:
                        pass        # 只是顯示用,抓不到下次再試
                self.auth = "ok"
                if len(ids) >= max_list:
                    # Gmail 由新到舊列,超過上限的是最舊的那些。last_poll 不前進:下次的 after:
                    # 才會再涵蓋它們(調高上限後補齊)
                    self.last_error = (f"信件超過 {max_list} 封,較舊的沒抓到;"
                                       "請調高 data/config/xfer690.json 的 max_list")
                else:
                    self.last_poll = self.now().isoformat()
                    self.last_error = None
                self.last_poll_kind = kind
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
                "events": self.events, "last_poll": self.last_poll, "account": self.account,
                "last_poll_kind": self.last_poll_kind, "traffic": self.traffic,
                "wiki": {"last_write": self.wiki["last_write"], "last_login": self.wiki["last_login"],
                         "account": self.wiki["account"]}})
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
            "auth": self.auth, "gmail_account": self.account,
            "last_poll": self.last_poll, "last_poll_kind": self.last_poll_kind,
            "last_error": self.last_error, "stale": stale,
            "poll_interval_min": int(self.cfg["poll_interval_min"]),
            "traffic": {"today_bytes": traffic["today_bytes"],
                        "last_poll_bytes": traffic["last_poll_bytes"]},
            "status": self.status(),
            "recent": recent,
            "wiki": {"enabled": bool(w.get("enabled")), "dry_run": bool(w.get("dry_run", True)),
                     "url": w.get("url"), "logged_in": self._jwt is not None,
                     "account": self.wiki["account"] if self._jwt else None,
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
    def wiki_login(self, pasted) -> dict:
        """使用者貼上瀏覽器的 jwt cookie(Innovue OAuth 登入後才有)。

        先問 Wiki「這是誰」確認有效,才存進 secrets 檔(wiki_jwt)。回應不回顯 token。"""
        from xfer690.wiki import WikiAuthError, WikiError
        with self._lock:        # 改了 xfer690.json(例如打開 wiki.enabled)直接按登入就生效
            self.cfg = load_xfer_config(self.config_path)
        w = self.cfg["wiki"]
        with self._wiki_lock:
            if not w.get("enabled"):
                return self._login_result(False, "Wiki 功能還沒啟用(xfer690.json 的 wiki.enabled)")
            token = _clean_jwt(pasted)
            if not token:
                return self._login_result(False, "貼上的不是 jwt:要複製 Cookies 裡名稱是 jwt 的那一列的「值」")
            try:
                client = self.wiki_factory(w["url"], token)
                who = client.whoami()
            except WikiAuthError:
                return self._login_result(False, "Wiki 不認得這個 jwt(可能過期或複製不完整),請重新登入 Wiki 後再複製一次")
            except WikiError as e:
                return self._login_result(False, f"連不上 Wiki:{e}"[:200])
            except Exception as e:
                return self._login_result(False, f"連不上 Wiki:{type(e).__name__}")
            self.wiki["account"] = (f"{who['name']}({who['email']})" if who["name"] and who["email"]
                                    else who["name"] or who["email"])
            self._set_jwt(client.token)
            res = self._login_result(True, "")
            self._save()
        self._refresh_preview()
        return res

    def wiki_logout(self) -> dict:
        with self._wiki_lock:
            self._set_jwt(None)
            self.wiki.update(tables=[], report=None, account=None)
            self._contents = {}
            self._save()
        return {"ok": True}

    def _set_jwt(self, token) -> None:
        """換 token(登入、Wiki 續期、失效),同步寫回 secrets 檔;檔裡其他欄位不動。"""
        self._jwt = token
        secrets = read_json(self.secrets_path, default={})
        secrets = secrets if isinstance(secrets, dict) else {}
        if token:
            if secrets.get("wiki_jwt") == token:
                return
            secrets["wiki_jwt"] = token
        elif "wiki_jwt" in secrets:
            del secrets["wiki_jwt"]
        else:
            return
        try:
            write_json_atomic(self.secrets_path, secrets)
        except OSError as e:
            self.wiki["last_error"] = f"登入憑證寫不進 {self.secrets_path.name}:{e}"[:300]

    def _login_result(self, ok: bool, message: str) -> dict:
        self.wiki["last_login"] = {"at": self.now().isoformat(), "ok": ok, "message": message}
        return {"ok": ok, "message": message}

    def _expire(self, message: str) -> None:
        self._set_jwt(None)
        self.wiki["last_login"] = {"at": self.now().isoformat(), "ok": False, "message": message}
        self._save()

    def _refresh_preview(self) -> None:
        """登入狀態下讀頁面、算預覽(不寫)。任何錯誤只記在 wiki.last_error。"""
        from xfer690.wiki import WikiAuthError, sync
        with self._wiki_lock:
            if not self._jwt:
                return
            try:
                client = self.wiki_factory(self.cfg["wiki"]["url"], self._jwt)
                try:
                    res = sync(client, self.status(), self.cfg["wiki"], write=False)
                finally:
                    if client.token != self._jwt:       # Wiki 續期發的新 token
                        self._set_jwt(client.token)
                self._contents = res["contents"]
                self.wiki.update(report=res["pages"], tables=res["tables"],
                                 last_sync=self.now().isoformat(), last_error=_all_pages_failed(res["pages"]))
            except WikiAuthError:
                self._expire(EXPIRED_MSG)
            except Exception as e:
                self.wiki["last_error"] = f"讀 Wiki 失敗:{e}"[:300]

    def wiki_write(self, edits=(), only=None) -> dict:
        """用記憶體裡的 JWT 寫入(使用者按「寫入 Wiki」)。沒有 JWT → NotLoggedIn。

        only={"page", "table", "key"}:「只寫這一列」,只動那一頁那張表的那一列。"""
        from xfer690.wiki import WikiAuthError, sync
        edits = [e for e in (edits or []) if isinstance(e, dict)]
        if only is not None:
            known = {(t["page"], t.get("table") or "") for t in self.cfg["wiki"].get("tables", [])}
            if (not isinstance(only, dict) or not isinstance(only.get("key"), str) or not only["key"]
                    or (only.get("page"), only.get("table")) not in known):
                return {"ok": False, "message": "「只寫這一列」指定的表或列不對"}
            only = {"page": only["page"], "table": only["table"], "key": only["key"]}
        with self._wiki_lock:
            if not self._jwt:
                raise NotLoggedIn()
            try:
                client = self.wiki_factory(self.cfg["wiki"]["url"], self._jwt)
                try:
                    res = sync(client, self.status(), self.cfg["wiki"], edits=edits, write=True, only=only)
                finally:
                    if client.token != self._jwt:
                        self._set_jwt(client.token)
            except WikiAuthError:
                self._expire(EXPIRED_MSG)
                raise NotLoggedIn()
            except Exception as e:
                self.wiki["last_error"] = f"寫 Wiki 失敗:{e}"[:300]
                return {"ok": False, "message": self.wiki["last_error"]}
            pages = res["pages"]
            if only:        # 只動了一張表:其他表的預覽照舊,這張表換成寫入後的結果
                self._contents.update(res["contents"])
                same = lambda t: t["page"] == only["page"] and (t.get("table") or "") == only["table"]
                fresh = [t for t in res["tables"] if same(t)]
                tables = ([t for t in self.wiki["tables"] if not same(t)] + fresh) if fresh else self.wiki["tables"]
            else:
                self._contents = res["contents"]
                tables = res["tables"]
            self.wiki.update(report=pages, tables=tables, last_sync=self.now().isoformat(),
                             last_error=None)
            self.wiki["last_write"] = {
                "at": self.now().isoformat(),
                "pages": sum(1 for p in pages.values() if p.get("updated")),
                "filled": sum(p.get("filled", 0) for p in pages.values()),
                "inserted": sum(p.get("inserted", 0) for p in pages.values()),
                "edited": sum(p.get("edited", 0) for p in pages.values()),
                "dry_run": bool(self.cfg["wiki"].get("dry_run", True)),
                "only": f"{only['page']}{(' ' + only['table']) if only['table'] else ''} {only['key']}" if only else None}
            self._save()
            return {"ok": True, "dry_run": bool(self.cfg["wiki"].get("dry_run", True)),
                    "pages": pages, "last_write": self.wiki["last_write"]}
