"""690 資轉監控的組裝:設定、抓信、快取、狀態、KM。

不在背景定時抓信:網頁打開時自動抓一次,之後只在按「立即更新」(或 Gmail 授權完成)時抓。

流量預算(這套工具本身就在管每天 2 GB 的額度):一律先抓 metadata(主旨、時間,每封約 2 KB),
只有三種信才下載全文 —— 失敗信、期別系統每段的第一封(帶出 VOL)、要填筆數的步驟。

KM 只在使用者按「寫入 KM」時寫;JWT 存在 secrets 檔的 wiki_jwt,
登入、續期與失效時同步更新,不進 snapshot 或事件快取、不回顯到網頁。
"""

import copy
import json
import re
import threading
from datetime import datetime, timedelta

from common.jsonio import read_json, write_json_atomic
from core.paths import (GMAIL_CREDENTIALS_PATH, GMAIL_TOKEN_PATH, XFER690_CACHE_PATH,
                        XFER690_CONFIG_PATH, XFER690_SECRETS_PATH)
from xfer690.parser import TPE, log_text, parse_raw, parse_subject, sud_types
from xfer690.status import build_status, date_log_runs, group_runs

# 預設值。systems / tables 留空:實際的系統清單在 data/config/xfer690.json(進版控)。
DEFAULT_XFER_CONFIG = {
    "enabled": True,
    "retain_days": 45,
    "history_days": 14,
    "project": "s690",
    "gmail_query": 'list:tipo.innovue.ltd subject:"[s690]"',
    "status_map": {"SUCCESS": "ok", "FAILURE": "fail"},
    "nodata_re": "GCS 資料夾無檔案",
    "max_raw_kb": 512,
    "max_list": 5000,       # 一次最多列幾封(45 天約 740 封)
    "s690_enabled": True,      # 抓完信後查 s690(KM 的 690查詢筆數、SUD 抽測、TW影像)
    "systems": [],
    "wiki": {
        "enabled": False,
        "dry_run": True,
        "url": "https://km.innovue.ltd",
        "locale": "zh-tw",
        "placeholder_re": r"^\d{4}x{4}$",
        "nodata_value": "-",        # 每日系統「GCS 資料夾無檔案」那天,要打 v 的格改填這個
        "sud_ok_value": "成功",     # SUD 抽測比對正確時「備註」填這個
        "base": "QC部/40_其他專案系統/智慧局-690整合式專利檢索系統/資料轉置登記",
        "tables": [],
    },
}

META_BYTES = 2048           # 一封 metadata 的流量估算
RECENT = 20
# s690 剛變成整期成功時可能還沒建好那期的索引:0 筆 / 查不到案件的暫定結果,24 小時內每次抓信再查
S690_SETTLE = timedelta(hours=24)
# 抽查的判定規則版本(要跟 s690.RULE_VERSION 一樣);存的結果版本不同就重查一次
# 2:公告(TWB)代表圖改認 alt 以 * 開頭的圖式(2026-10-08)
S690_RULE = 2
AUTH_WAIT = timedelta(minutes=10)      # 按「授權」後等 Google 導回來的時間
AUTH_MSG = "Gmail 授權已失效或被管理員封鎖,請在「信件與連線」按「重新授權」"
_MSG_ID_RE = re.compile(r"^[0-9a-fA-F]{6,32}$")
# 一輪裡抓失敗幾封就不再抓(多半是網路斷了,不要每封都等逾時);已抓到的照樣存
MAX_MISSED = 3


def _note_missed(missed: list, e: Exception) -> None:
    """記下一封沒抓到的信。授權失效不是單封的問題,往外拋讓整輪改成「要重新授權」。"""
    if type(e).__name__ == "AuthError":
        raise e
    missed.append(str(e) or type(e).__name__)


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


EXPIRED_MSG = "KM 登入已失效,請重新用 Innovue OAuth 登入 KM 後再貼一次 jwt"
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")


def _all_pages_failed(pages: dict):
    """每一頁都讀失敗(例如被 Cloudflare 擋)時,回傳要顯示的錯誤;否則 None。

    sync 把每頁的錯誤收在 report 裡、不往外拋,全部失敗時畫面上要看得到原因。"""
    errs = [p["errors"][0] for p in pages.values() if p.get("errors")]
    if pages and len(errs) == len(pages):
        return f"讀 KM 失敗:{errs[0]}"[:300]
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


def _default_flow(credentials_path, redirect_uri):
    from xfer690.authorize import new_flow
    return new_flow(credentials_path, redirect_uri)


def _default_wiki(url, token=None):
    from xfer690.wiki import WikiClient
    return WikiClient(url, token)


def _default_s690():
    from xfer690.s690 import S690Client
    return S690Client()


class Xfer690Service:
    def __init__(self, config_path=XFER690_CONFIG_PATH, cache_path=XFER690_CACHE_PATH,
                 secrets_path=XFER690_SECRETS_PATH, credentials_path=GMAIL_CREDENTIALS_PATH,
                 gmail_factory=_default_gmail, wiki_factory=_default_wiki, s690_factory=_default_s690,
                 now=lambda: datetime.now(TPE)):
        self.config_path = config_path
        self.cache_path = cache_path
        self.secrets_path = secrets_path
        self.credentials_path = credentials_path
        self.gmail_factory = gmail_factory
        self.wiki_factory = wiki_factory
        self.s690_factory = s690_factory
        self._s690_lock = threading.Lock()    # 背景查 s690 同時只跑一個
        self._spawn = lambda fn: threading.Thread(target=fn, daemon=True).start()   # 測試可換
        self.token_path = GMAIL_TOKEN_PATH    # 測試可換
        self.flow_factory = _default_flow     # Google 同意頁的流程物件(測試可換)
        self._pending = None                  # 按了「授權」、還在等 Google 導回來:{flow, state, until}
        self.now = now
        self.cfg = load_xfer_config(config_path)
        self._lock = threading.Lock()         # 整個 poll()
        self._wiki_lock = threading.Lock()    # 登入 / 寫入 / 預覽
        self._jwt = None                      # KM 登入憑證;也存在 secrets 檔(重開不用重貼)

        cache = read_json(cache_path, default={})
        cache = cache if isinstance(cache, dict) else {}
        self.events = cache.get("events") if isinstance(cache.get("events"), dict) else {}
        self.last_poll = cache.get("last_poll")
        self.last_poll_kind = cache.get("last_poll_kind")
        self.last_error = None
        self.auth = "unknown"
        self.account = cache.get("account")
        s = cache.get("s690")
        self.s690 = s if isinstance(s, dict) else {}    # {"twb:5328": {"count"/"image"/"sud": {…, "at"}}}
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
            self._jwt = jwt     # 上次貼的;失效的話第一次讀 KM 時清掉

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
        # 英譯的列鍵(期數區間)在 log 的 FILE_I / FILE 裡,跟期別系統一樣每段要一封
        span = {t["system"] for t in self.cfg["wiki"].get("tables", []) if t.get("key") == "span"}
        for s in self.cfg["systems"]:
            if not str(s.get("period", "")).startswith("vol") and s["id"] not in span:
                continue
            codes = set(s.get("auto", [])) | set(s.get("steps", []))
            evs = [e for e in list(self.events.values()) + list(new.values())
                   if e.get("code") in codes and e.get("at")]
            for seg in group_runs(evs, s):
                # 每段只要一封帶出 VOL(或 FILE):取第一封子步驟(母 job 的信排在最後,通常不是第一封)
                first = next((e for e in seg if e["code"] in s.get("steps", [])), seg[0])
                if any(e.get("raw") for e in seg if e["id"] not in new):
                    continue
                if first["id"] in new:
                    want.add(first["id"])
        return want | {e["id"] for e in self._date_log_wanted(new)}

    def _date_log_wanted(self, new: dict) -> list:
        """每日系統的補跑(rerun_date):每次執行要一封有 log 的信,才知道補的是哪一天。
        之前抓過、還沒下載 log 的信(這個設定加上去以前收到的)也包含在內,補下載一次。"""
        out = []
        for s in self.cfg["systems"]:
            codes = set(s.get("auto", [])) | set(s.get("steps", []))
            evs = [e for e in list(self.events.values()) + list(new.values())
                   if e.get("code") in codes and e.get("at")]
            for seg in date_log_runs(evs, s):
                if any(e.get("raw") for e in seg):
                    continue
                out.append(next((e for e in seg if e["code"] in s.get("steps", [])), seg[0]))
        return out

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
                new, missed = {}, []
                for mid in ids:
                    if mid in self.events or len(missed) >= MAX_MISSED:
                        continue
                    try:
                        meta = gmail.fetch_meta(mid)
                    except Exception as e:
                        _note_missed(missed, e)
                        continue
                    spent += META_BYTES
                    ev = parse_subject(meta["subject"], meta["date"], mid, self.cfg)
                    ev["size"] = meta["size"]
                    new[mid] = ev
                limit = int(self.cfg["max_raw_kb"]) * 1024
                for mid in sorted(self._raw_wanted(new)):
                    ev = new.get(mid) or self.events[mid]
                    if not ev.get("code") or ev["size"] > limit or len(missed) >= MAX_MISSED:
                        continue
                    try:
                        raw = gmail.fetch_raw(mid)
                    except Exception as e:
                        _note_missed(missed, e)
                        new.pop(mid, None)      # 新信整封不收,下次重新列到再抓;舊信下次還會要 log
                        continue
                    spent += ev["size"] or len(raw)
                    try:
                        new[mid] = parse_raw(raw, ev, self.cfg)
                    except Exception as e:      # 解析不了的信照樣留下 metadata
                        ev["error"] = f"解析失敗:{e}"[:200]
                # 換整個 dict(不就地改):snapshot / status 不拿鎖,正在走訪舊的也不會出錯
                self.events = dict(self.events, **new)
                self._prune()
                if not self.account and hasattr(gmail, "account"):
                    try:
                        self.account = gmail.account()
                    except Exception:
                        pass        # 只是顯示用,抓不到下次再試
                self.auth = "ok"
                if missed:
                    # 有信沒抓到:已抓到的照樣存;last_poll 不前進,下次的 after: 才會再列到沒抓到的
                    self.last_error = f"{len(missed)} 封信沒抓到,下次抓信再補:{missed[0]}"[:300]
                elif len(ids) >= max_list:
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
        if self.cfg.get("s690_enabled", True):
            self._spawn(self._s690_bg)      # 查 s690 要幾秒到幾十秒,不讓「立即更新」等它
        return self.snapshot()

    def _prune(self) -> None:
        cut = self.now() - timedelta(days=int(self.cfg["retain_days"]))
        self.events = {m: e for m, e in self.events.items()
                       if not (e.get("at") and datetime.fromisoformat(e["at"]) < cut)}
        cut_at = cut.isoformat()
        self.s690 = {k: v for k, v in self.s690.items()
                     if max((x.get("at") or "" for x in v.values()), default="") >= cut_at}

    # ── s690(KM 的 690查詢筆數、SUD 抽測、TW影像) ──
    def _s690_jobs(self, status: dict) -> list:
        """[(快取鍵, 部分, 案件類型, 期)]:整期成功、還沒有成功結果,或之後又重跑過的。"""
        jobs = []
        for s in status.get("systems", []):
            if s.get("type") not in ("biblio", "sud") or s.get("view") not in ("twa", "twb"):
                continue
            parts = ("count", "image") if s["type"] == "biblio" else ("sud",)
            for p in s.get("periods", []):
                if p.get("state") != "ok" or not p.get("last_at"):
                    continue
                key = f"{s['view']}:{p['key']}"
                young = self.now() < datetime.fromisoformat(p["last_at"]) + S690_SETTLE
                have =self.s690.get(key) or {}
                for part in parts:
                    if part == "count" and not p.get("date"):
                        continue        # 沒有日期就不查(不送 PD:[None TO None])
                    r = have.get(part)
                    if (r and not r.get("error") and (r.get("at") or "") >= p["last_at"]
                            and r.get("rule") == S690_RULE):     # 判定規則改過的舊結果要重查
                        if not (self._s690_provisional(part, r) and young):
                            continue
                    jobs.append((key, part, s["view"].upper(), p))
        return jobs

    @staticmethod
    def _s690_provisional(part: str, r: dict) -> bool:
        """暫定結果:0 筆、或查不到案件(s690 可能還沒建好那期的索引)。"""
        return r.get("value") == 0 if part == "count" else not r.get("pn")

    def _s690_refresh(self) -> bool:
        """查還缺的、存進快取。有查回 True。已經有一個在跑就直接回 False。"""
        if not self._s690_lock.acquire(blocking=False):
            return False
        try:
            jobs = self._s690_jobs(self.status())
            if not jobs:
                return False
            client = self.s690_factory()
            for key, part, ct, p in jobs:
                try:
                    if part == "count":
                        r = {"value": client.count(ct, p["date"])}
                    elif part == "image":
                        r = client.image_check(ct, p["key"])
                    else:
                        r = client.sud_sample(ct, p["key"], self._sud_types_of(p))
                except Exception as e:      # 連不上、改版:記下來,下次抓信再試
                    r = {"error": str(e)[:200] or type(e).__name__}
                r["at"] = self.now().isoformat()
                r["rule"] = S690_RULE
                with self._lock:
                    # 換整個 dict(不就地改):別的執行緒正在序列化舊的也不會出錯
                    entry = dict(self.s690.get(key) or {}, **{part: r})
                    self.s690 = dict(self.s690, **{key: entry})
                    self._save()
            return True
        finally:
            self._s690_lock.release()

    def _sud_types_of(self, p: dict) -> list:
        """這期的雜項類型。這個功能之前抓的信沒有,補下載那期一封有 log 的信取一次(約 20–60 KB)。"""
        if p.get("sud_types"):
            return sorted(p["sud_types"])
        for st in (p.get("steps") or {}).values():
            ev = st.get("event") or {}
            if not (ev.get("raw") and ev.get("id") in self.events):
                continue
            raw = self.gmail_factory().fetch_raw(ev["id"])
            self._add_bytes(len(raw))
            types = sud_types(log_text(raw))
            if types:
                with self._lock:
                    if ev["id"] in self.events:     # 等鎖的時候可能剛好被 _prune 清掉
                        self.events = dict(self.events, **{ev["id"]: dict(self.events[ev["id"]], sud_types=types)})
                return sorted(types)
        return []

    def _s690_bg(self) -> None:
        from core.health import record_error
        try:
            if self._s690_refresh() and self._jwt:
                self._refresh_preview()
        except Exception as e:
            record_error("xfer690", e)

    def _save(self) -> None:
        try:
            write_json_atomic(self.cache_path, {
                "events": self.events, "last_poll": self.last_poll, "account": self.account,
                "last_poll_kind": self.last_poll_kind, "traffic": self.traffic, "s690": self.s690,
                "wiki": {"last_write": self.wiki["last_write"], "last_login": self.wiki["last_login"],
                         "account": self.wiki["account"]}})
        except OSError as e:
            self.last_error = f"快取寫不進去:{e}"

    # ── 畫面 ────────────────────────────────────────────────
    def status(self) -> dict:
        st = build_status(list(self.events.values()), self.cfg["systems"], self.now(),
                          int(self.cfg["history_days"]))
        st["s690"] = self.s690      # KM 預覽用(wiki.plan_fills / s690_note)
        st["s690_enabled"] = bool(self.cfg.get("s690_enabled", True))
        return st

    def snapshot(self) -> dict:
        now = self.now()
        recent = sorted((e for e in self.events.values() if e.get("code") and e.get("at")),
                        key=lambda e: e["at"], reverse=True)[:RECENT]
        w = self.cfg["wiki"]
        traffic = dict(self.traffic)
        if traffic.get("day") != now.date().isoformat():
            traffic["today_bytes"] = 0
        return {
            "ok": True, "enabled": bool(self.cfg.get("enabled", True)),
            "auth": self.auth, "gmail_account": self.account,
            # 有 token、而且沒被判定失效 = 已授權(卡片顯示「登出」;否則顯示「授權」)
            "gmail_authorized": self.token_path.exists() and self.auth != "needs_auth",
            "gmail_authorizing": self._authorizing(),
            "last_poll": self.last_poll, "last_poll_kind": self.last_poll_kind,
            "last_error": self.last_error,
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

    # ── Gmail 授權(信件與連線的按鈕) ──
    # Google 同意後直接回呼主程式的 /api/xfer690/gmail/callback(這個伺服器一直開著、每個連線
    # 各一條執行緒)。不另開暫時伺服器:google 套件的那個只接一個連線,瀏覽器的預先連線佔掉
    # 以後真正的回呼進不來,畫面會一直「等待授權」到逾時。
    def gmail_authorize(self, redirect_uri: str) -> dict:
        """產生 Google 同意頁網址(網頁在新分頁開)。再按一次就換一組新的,舊的作廢。"""
        if not self.credentials_path.exists():
            return {"ok": False, "message": f"找不到 {self.credentials_path.name},請照 690授權.bat 的說明設定"}
        try:
            flow = self.flow_factory(self.credentials_path, redirect_uri)
            url, state = flow.authorization_url(prompt="consent")   # consent:重新授權才會再給 refresh_token
        except ImportError:
            return {"ok": False, "message": "缺少套件 google-auth-oauthlib,請執行 python -m pip install -r requirements-690.txt"}
        except Exception as e:
            return {"ok": False, "message": f"開不了授權頁:{e}"[:300]}
        self._pending = {"flow": flow, "state": state, "until": self.now() + AUTH_WAIT}
        return {"ok": True, "url": url}

    def _authorizing(self) -> bool:
        return bool(self._pending) and self.now() < self._pending["until"]

    def gmail_callback(self, query: dict) -> str:
        """Google 同意頁導回來。回傳要顯示在那個分頁的訊息。"""
        p = self._pending
        if not p or not query.get("state") or query.get("state") != p["state"]:
            return "這個授權連結已經失效,請回到「信件與連線」再按一次「授權」。"
        self._pending = None
        if query.get("error"):
            self.last_error = f"Gmail 授權沒有完成:{query['error']}"[:300]
            return f"授權沒有完成({query['error']}),可以關掉這個分頁,回到「信件與連線」再試一次。"
        try:
            p["flow"].fetch_token(code=query.get("code") or "")
            write_json_atomic(self.token_path, json.loads(p["flow"].credentials.to_json()))
        except Exception as e:
            self.last_error = f"Gmail 授權沒有完成:{e}"[:300]
            return f"授權沒有完成:{e}"[:300]
        self.auth = "unknown"
        self.account = None             # 可能換了帳號,下次抓信重讀
        self.last_error = None
        # 抓信可能要一陣子,不讓這個分頁等;網頁看到 auth 是 unknown 會每 3 秒重讀
        threading.Thread(target=self.poll, args=("manual",), daemon=True).start()
        return "授權完成,可以關掉這個分頁。"

    def gmail_logout(self) -> dict:
        """刪掉這台電腦的 Gmail token,停止抓信,直到重新授權。"""
        with self._lock:
            try:
                self.token_path.unlink()
            except FileNotFoundError:
                pass
            self.auth = "needs_auth"
            self.account = None
            self.last_error = None
            self._save()
        return {"ok": True}

    # ── KM ────────────────────────────────────────────────
    def wiki_login(self, pasted) -> dict:
        """使用者貼上瀏覽器的 jwt cookie(Innovue OAuth 登入後才有)。

        先問 KM「這是誰」確認有效,才存進 secrets 檔(wiki_jwt)。回應不回顯 token。"""
        from xfer690.wiki import WikiAuthError, WikiError
        with self._lock:        # 改了 xfer690.json(例如打開 wiki.enabled)直接按登入就生效
            self.cfg = load_xfer_config(self.config_path)
        w = self.cfg["wiki"]
        with self._wiki_lock:
            if not w.get("enabled"):
                return self._login_result(False, "KM 功能還沒啟用(xfer690.json 的 wiki.enabled)")
            token = _clean_jwt(pasted)
            if not token:
                return self._login_result(False, "貼上的不是 jwt:要複製 Cookies 裡名稱是 jwt 的那一列的「值」")
            try:
                client = self.wiki_factory(w["url"], token)
                who = client.whoami()
            except WikiAuthError:
                return self._login_result(False, "KM 不認得這個 jwt(可能過期或複製不完整),請重新登入 KM 後再複製一次")
            except WikiError as e:
                return self._login_result(False, f"連不上 KM:{e}"[:200])
            except Exception as e:
                return self._login_result(False, f"連不上 KM:{type(e).__name__}")
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
            self._save()
        return {"ok": True}

    def _set_jwt(self, token) -> None:
        """換 token(登入、KM 續期、失效),同步寫回 secrets 檔;檔裡其他欄位不動。"""
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
                    if client.token != self._jwt:       # KM 續期發的新 token
                        self._set_jwt(client.token)
                self.wiki.update(report=res["pages"], tables=res["tables"],
                                 last_sync=self.now().isoformat(), last_error=_all_pages_failed(res["pages"]))
            except WikiAuthError:
                self._expire(EXPIRED_MSG)
            except Exception as e:
                self.wiki["last_error"] = f"讀 KM 失敗:{e}"[:300]

    def wiki_write(self, edits=()) -> dict:
        """用記憶體裡的 JWT 寫入(使用者按「寫入 KM」)。沒有 JWT → NotLoggedIn。"""
        from xfer690.wiki import WikiAuthError, sync
        edits = [e for e in (edits or []) if isinstance(e, dict)]
        with self._wiki_lock:
            if not self._jwt:
                raise NotLoggedIn()
            try:
                client = self.wiki_factory(self.cfg["wiki"]["url"], self._jwt)
                try:
                    res = sync(client, self.status(), self.cfg["wiki"], edits=edits, write=True)
                finally:
                    if client.token != self._jwt:
                        self._set_jwt(client.token)
            except WikiAuthError:
                self._expire(EXPIRED_MSG)
                raise NotLoggedIn()
            except Exception as e:
                self.wiki["last_error"] = f"寫 KM 失敗:{e}"[:300]
                return {"ok": False, "message": self.wiki["last_error"]}
            pages = res["pages"]
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
