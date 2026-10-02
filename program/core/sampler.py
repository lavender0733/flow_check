"""流量取樣與計費。

從 traffic_monitor.py 抽出。核心設計（原本寫在 traffic_monitor.py 的檔頭）：

讀網卡「累積」計數器而非取樣速率 —— 計數器單調遞增，所以 current - last
永遠精確，不論多久取樣一次都不會漏掉中間的流量。重開機會讓計數器歸零，
偵測到 current < last 時就把當前值當成重開後的增量。

另含主動測速（run_speedtest）與兩個背景迴圈：sampler_loop 依 sample_interval_sec
結算 NIC 用量，probe_loop 以高得多的頻率（lan_probe_interval_sec）用 ESTATS
取每條 TCP 連線的收/發位元組，供內網排除與暴增歸因。兩者刻意解耦。
"""

import collections
import ipaddress
import json
import threading
import time
import urllib.request
from datetime import datetime

import psutil

from common.jsonio import read_json, write_json_atomic
from core import daily_stats
from core.estats import process_is_elevated, estats_probe
from core.health import record_error
from core.procinfo import describe as describe_process, describe_exe_name
from core.paths import DATA_DIR, MACHINE_ID, STATE_PATH, TRAFFIC_LOG_DIR
from core.storage import load_prefs


# 今日程式排行裡兩個「不是程式」的桶。鍵以雙底線包起來,不會跟真的程序名撞在一起。
# UNATTRIBUTED:NIC 確實計到、但 ESTATS 沒對到任何 TCP 連線的量 —— UDP/QUIC(Chrome、YouTube、
#   視訊會議大量走它)、TCP/IP 標頭與 ACK、沒提權時的全部流量。以前這部分直接消失,
#   「沒看到暴增卻用完」多半就藏在這裡;現在單獨列一桶,至少看得到它有多大。
# OFFLINE:監控程式沒在跑(關掉、重開機)那段期間的量。重啟時網卡計數器的差額一次補進來,
#   以前會被當成重啟後第一分鐘的流量,看起來像莫名的暴增、又歸不到任何程式。
UNATTRIBUTED = "__unattributed__"
OFFLINE = "__offline__"

# 逐分鐘明細檔保留天數(本機 LOCALAPPDATA,不進雲端同步)。
TRAFFIC_LOG_KEEP_DAYS = 31
# 一次取樣「未歸因」超過這個量時,順手記下當時持有 UDP 通訊埠的程式當線索(QUIC 抓不到逐連線位元組)。
UDP_HINT_BYTES = 20 * 1024 * 1024


def traffic_log_path(date: str):
    return TRAFFIC_LOG_DIR / f"traffic_{date}.jsonl"


def read_traffic_log(date: str) -> list:
    """讀某一天的逐分鐘明細(舊到新)。檔不在或壞行一律略過。"""
    rows = []
    try:
        with traffic_log_path(date).open(encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    rows.append(rec)
    except OSError:
        pass
    return rows


def traffic_log_dates() -> list:
    """有逐分鐘明細的日期(新到舊)。"""
    try:
        names = [p.stem[len("traffic_"):] for p in TRAFFIC_LOG_DIR.glob("traffic_*.jsonl")]
    except OSError:
        return []
    return sorted((n for n in names if len(n) == 10), reverse=True)


def _prune_traffic_logs(keep_days: int = TRAFFIC_LOG_KEEP_DAYS):
    for d in traffic_log_dates()[keep_days:]:
        try:
            traffic_log_path(d).unlink()
        except OSError:
            pass


def _udp_owner_names() -> list:
    """目前持有非迴路 UDP 通訊埠的程式名(依持有數排序)。只當線索:UDP 沒有逐連線位元組可查。"""
    names = collections.Counter()
    try:
        for c in psutil.net_connections(kind="udp"):
            ip = c.laddr.ip if c.laddr else ""
            if not c.pid or ip.startswith("127.") or ip == "::1":
                continue
            try:
                names[psutil.Process(c.pid).name()] += 1
            except Exception:
                continue
    except Exception:
        return []
    return [n for n, _ in names.most_common(8)]


def _upgrade_surge(ev: dict) -> dict:
    """升級前存下的暴增事件:來源只有程序檔名(chrome.exe)、也沒有「未歸因」那一列。
    讀檔時補齊成新格式 —— 檔名換成軟體名稱,事件總量減掉各來源加總的差額補成「未歸因」,
    網頁顯示才會和新紀錄一致。已是新格式(來源帶 exe 欄)的原樣回傳。"""
    top = ev.get("top")
    if not isinstance(top, list) or any("exe" in t for t in top if isinstance(t, dict)):
        return ev
    names = {}
    new_top = []
    for t in top:
        if not isinstance(t, dict):
            continue
        exe = str(t.get("name") or "")
        if exe not in names:
            names[exe] = describe_exe_name(exe)
        new_top.append(dict(t, name=names[exe], exe=exe))
    rest = int(ev.get("bytes", 0)) - sum(int(t.get("bytes", 0)) for t in new_top)
    if rest > 0:
        new_top.append({"ip": "—", "name": UNATTRIBUTED, "pid": None,
                        "bytes": rest, "recv": None, "sent": None})
    new_top.sort(key=lambda x: x.get("bytes", 0), reverse=True)
    return dict(ev, top=new_top)


# ============================================================
# 流量監控
# ============================================================
class Monitor:
    """Accumulates per-day network usage (all NICs summed)."""

    def __init__(self, cfg: dict, counters_fn=None):
        self.cfg = cfg
        # 網卡累積計數器的來源。預設讀 psutil;測試時可注入假資料,
        # 才驗證得了跨日歸零與「重開機計數器回捲」這兩段核心邏輯。
        self._counters_fn = counters_fn or (lambda: psutil.net_io_counters(pernic=True))
        self.quota_bytes = float(cfg["quota_gb"]) * (1024 ** 3)
        self.debug_lan = bool(cfg.get("debug_lan", False))
        self.exclude = [x.lower() for x in cfg.get("exclude_nics", [])]
        self.lock = threading.Lock()

        self.date = None
        self.day_sent = 0
        self.day_recv = 0
        self.per_nic = {}        # nic -> [sent, recv]  (today's accumulated)
        self.last_per_nic = {}   # nic -> [sent, recv]  (last cumulative read)
        self.last_time = None
        self.last_sample_at = None   # 最後一次「成功取樣」的牆鐘時間(供頁面判斷是否停更)
        self.rate_up = 0.0
        self.rate_down = 0.0

        # 被動限速偵測:留一段時間內的下載速率,取其最高值當「天花板」。
        # 只有真的在下載時這個峰值才有意義(閒置時偏低,不代表被限速)。
        self.peak_window_sec = float(cfg.get("peak_window_min", 10)) * 60
        self.rate_hist = collections.deque()   # (monotonic_ts, rate_down_bytes_per_sec)

        # ── 流量暴增預警 ──
        # surge_enabled / surge_min_mb 以本機 prefs 為準(頁面可切),其餘參數讀 config。
        interval = max(1, int(cfg.get("sample_interval_sec", 60)))
        prefs = load_prefs()
        se = prefs.get("surge_enabled")
        self.surge_enabled = bool(se) if se is not None else bool(cfg.get("surge_enabled", True))
        mb = prefs.get("surge_min_mb")
        base_mb = float(mb) if mb is not None else float(cfg.get("surge_min_mb", 50))
        self.surge_min_bytes = max(0.0, base_mb) * 1024 * 1024
        self.surge_factor = float(cfg.get("surge_factor", 4.0))
        self.surge_cooldown = float(cfg.get("surge_cooldown_sec", 300))
        win_sec = float(cfg.get("surge_window_min", 30)) * 60
        # 回看視窗存「每次取樣的用量(收+發)」,供中位數基準與暴增判斷;至少留 6 筆。
        self.byte_hist = collections.deque(maxlen=max(6, int(win_sec / interval)))
        # 當日暴增事件:跨重啟保留、跨日歸零。每個事件含當下每條 TCP 連線的 IP／程式／流量，
        # 供網頁「暴增紀錄」頁籤按應用程式／時間收合後逐來源展開；最多留 200 個避免狀態檔無限長大。
        self.surges = collections.deque(maxlen=200)
        self._last_surge_at = 0.0     # 最近一次記錄暴增的 monotonic 時刻(冷卻用)
        self._surge_seq = 0           # 事件流水號(前端據此判斷有沒有「新的」暴增)
        # 逐次取樣的計費明細:每筆 = 一次 NIC 結算怎麼分內/外網、算進額度多少、主要來源是誰。
        # 由伺服器每次取樣寫進本機明細檔(traffic_<日期>.jsonl)並留一整天在記憶體,
        # 不靠網頁開著 —— 網頁關著的時段、重開監控之後,明細都還在。
        self.sample_log = collections.deque(maxlen=(86400 + interval - 1) // interval + 60)
        # 今日各程式累計(計入額度的外網量):name -> [recv, sent]。暴增偵測只抓「單分鐘突然變大」,
        # 抓不到一整天細水長流的背景流量(雲端同步、備份、更新);這張排行才看得出誰累積吃最多。
        self.day_apps = {}
        # 重啟時若沿用當日存檔,上次存檔到這次啟動之間的流量(監控沒在跑)會在第一次取樣一次補進來;
        # 記下上次存檔時間,第一次取樣就把那筆標成「監控未執行期間」,不當成暴增、不亂歸因。
        self._resume_gap_from = None

        # ── 排除內網流量 ──
        # exclude_lan 以本機 prefs 為準(頁面可切);內網/強制計入網段清單讀 config。
        el = prefs.get("exclude_lan")
        self.exclude_lan_enabled = bool(el) if el is not None else bool(cfg.get("exclude_lan", True))
        self.lan_nets = []
        for c in cfg.get("lan_ranges", []):
            try:
                self.lan_nets.append(ipaddress.ip_network(c, strict=False))
            except ValueError:
                pass
        # 強制計入白名單(優先於 lan_ranges):保護落在內網網段、但實際走對外線路的流量。
        self.count_nets = []
        for c in cfg.get("count_ranges", []):
            try:
                self.count_nets.append(ipaddress.ip_network(c, strict=False))
            except ValueError:
                pass
        # 本機(loopback):127.x、::1。這種連線是程式跟同一台電腦上的另一支程式講話
        # (例如瀏覽器連本工具的 127.0.0.1:8765),封包根本不經過網卡、不吃額度。
        # 它不是內網也不是外網:以前沒有這一類,127.x 不在 lan_ranges 裡就被當成「外網」,
        # 於是灌進今日程式排行與暴增歸因(明明沒出網卡),還稀釋了內網排除的比例。
        self.local_nets = []
        for c in cfg.get("local_ranges", ["127.0.0.0/8", "::1/128"]):
            try:
                self.local_nets.append(ipaddress.ip_network(c, strict=False))
            except ValueError:
                pass
        self.day_lan_excluded = 0     # 今日已排除(內網)位元組;跨重啟保留、跨日歸零

        # ── 高頻 ESTATS 探測(與 60s NIC 計費解耦)──
        # probe_lan() 由獨立的高頻迴圈(lan_probe_interval_sec,預設 1s)呼叫:用 ESTATS 取每條
        # TCP 連線累積的收/發位元組,算出本探測區間的增量 → 內網連線的量累進 pending(供 sample()
        # 消耗、各方向 clamp 到實際 NIC 增量),各 IP／程序來源的外網增量累進 _pending_source_net 供暴增歸因。
        # 需提權才拿得到數字(_estats_ok);沒提權時內網排除與暴增歸因停用,但總流量計費照常。
        self._estats_ok = process_is_elevated()
        self._estats_prev = {}        # conn_key -> (data_in, data_out):上一輪各連線累計(算增量)
        # 自上次 NIC 取樣以來,各連線 ESTATS payload 增量,分內網/外網累計。sample() 用
        # 「內網 payload 占總 payload 的比例」去分攤實際 NIC 增量(含 TCP/IP 標頭、重傳),
        # 比舊的「payload 封頂」更準 —— 內網占滿時可扣掉近 100% NIC,不再漏那 ~4% 標頭。
        self._pending_lan_sent = 0    # 內網連線『上傳』payload
        self._pending_lan_recv = 0    # 內網連線『下載』payload
        self._pending_ext_sent = 0    # 外網連線『上傳』payload(含 count_ranges)
        self._pending_ext_recv = 0    # 外網連線『下載』payload
        # (pid, 對方 IP) -> [軟體名, recv, sent, 檔名]:自上次 NIC 取樣以來的外網來源增量,供歸因與明細。
        self._pending_source_net = {}
        self._name_cache = {}         # pid -> (軟體名, 檔名, 查詢時刻);避免每輪都查
        # 今日程式排行各軟體名對應的程序檔名(顯示在名稱旁,方便對照工作管理員)。
        self.day_app_exes = {}
        # 今日逐時用量(總量與報表那幾支程式),供流量報表展開到小時。格式見 core/daily_stats.py。
        self.day_hours = {}

        # 每日統計要分開記的程式(ssh / VS Code / Claude Code),見 core/daily_stats.py。
        self.report_apps = daily_stats.report_apps(cfg)

        self._load_state()

    def _record_day(self, date, day_recv, day_sent, lan_excluded, day_apps, day_app_exes, day_hours):
        """把某天的累計結算進每日統計。寫檔失敗只記錯誤,不影響取樣。"""
        try:
            daily_stats.record_day(date, daily_stats.from_day_state(
                self.report_apps, day_recv, day_sent, lan_excluded, day_apps, day_app_exes, day_hours))
        except OSError as e:
            record_error("daily_stats", e)

    def daily_stats(self) -> dict:
        """每日統計(舊到新),今天那筆用目前的累計即時算。"""
        days = daily_stats.load()
        with self.lock:
            days[self.date] = dict(daily_stats.from_day_state(
                self.report_apps, self.day_recv, self.day_sent, self.day_lan_excluded,
                self.day_apps, self.day_app_exes,
                {h: {k: list(v) for k, v in b.items()} for h, b in self.day_hours.items()}),
                source="live")
        return {"machine": MACHINE_ID, "apps": list(self.report_apps),
                "days": dict(sorted(days.items()))}

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _excluded(self, nic: str) -> bool:
        n = nic.lower()
        return any(x in n for x in self.exclude)

    def _baseline(self, counters: dict):
        self.last_per_nic = {
            nic: [st.bytes_sent, st.bytes_recv]
            for nic, st in counters.items() if not self._excluded(nic)
        }

    def _load_state(self):
        cur = self._counters_fn()
        today = self._today()
        saved = None
        # 讀新的 state_<機器>.json;若還沒有(剛從舊版升級),沿用舊的單一
        # state.json,讓今天已累積的流量無縫接續(下方網卡比對仍會擋掉別台資料)。
        for path in (STATE_PATH, DATA_DIR / "state.json"):
            if path.exists():
                saved = read_json(path, default=None)
                if saved is not None:
                    break

        # 換電腦偵測:存檔記的是「本機網卡名稱」。若存檔有網卡、卻沒有任何一張
        # 跟現在這台對得上,代表這份 state.json 是別台電腦複製過來的 ——
        # 不要沿用它的當日累計,改從 0 起算(避免顯示到別台電腦的流量)。
        cur_nics = {nic for nic in cur if not self._excluded(nic)}
        saved_nics = set((saved or {}).get("last_per_nic", {})) | set((saved or {}).get("per_nic", {}))
        same_machine = (not saved_nics) or bool(saved_nics & cur_nics)
        if saved and saved.get("date") == today and not same_machine:
            print("[info] 偵測到網卡與存檔不符(可能換了電腦),當日流量改從 0 起算。")

        # 存檔是前幾天的(監控跨日沒在跑):那天的累計還在存檔裡,先結算進每日統計再歸零。
        if saved and same_machine and saved.get("date") and saved.get("date") != today:
            self._record_day(saved["date"], saved.get("day_recv", 0), saved.get("day_sent", 0),
                             saved.get("day_lan_excluded", 0), saved.get("day_apps", {}),
                             saved.get("day_app_exes", {}), saved.get("day_hours"))

        if saved and saved.get("date") == today and same_machine:
            # Resume today's tally across a restart.
            self.date = today
            self.day_sent = int(saved.get("day_sent", 0))
            self.day_recv = int(saved.get("day_recv", 0))
            self.day_lan_excluded = int(saved.get("day_lan_excluded", 0))
            saved_apps = saved.get("day_apps", {})
            if isinstance(saved_apps, dict):
                self.day_apps = {str(k): [int(v[0]), int(v[1])] for k, v in saved_apps.items()
                                 if isinstance(v, list) and len(v) == 2}
            exes = saved.get("day_app_exes", {})
            if isinstance(exes, dict):
                self.day_app_exes = {str(k): str(v) for k, v in exes.items()}
            hours = saved.get("day_hours")
            if isinstance(hours, dict):
                self.day_hours = hours
            self._resume_gap_from = saved.get("saved_at") or "?"
            # 舊版 state 沒有 surges 時自然從空白開始;新版本保存當日完整來源紀錄供頁面展開。
            saved_surges = saved.get("surges", [])
            if isinstance(saved_surges, list):
                self.surges.extend(_upgrade_surge(ev) for ev in saved_surges if isinstance(ev, dict))
                self._surge_seq = max((int(ev.get("seq", 0)) for ev in self.surges), default=0)
            self.per_nic = {k: list(v) for k, v in saved.get("per_nic", {}).items()}
            self.last_per_nic = {k: list(v) for k, v in saved.get("last_per_nic", {}).items()}
            # Any NIC present now but not in saved state -> baseline it (don't
            # count its pre-existing cumulative history).
            for nic, st in cur.items():
                if not self._excluded(nic) and nic not in self.last_per_nic:
                    self.last_per_nic[nic] = [st.bytes_sent, st.bytes_recv]
        else:
            # Fresh day (or no/old state): start at zero, baseline from now.
            self.date = today
            self.day_sent = 0
            self.day_recv = 0
            self.day_lan_excluded = 0
            self.day_apps = {}
            self.day_app_exes = {}
            self.day_hours = {}
            self.surges.clear()
            self._surge_seq = 0
            self.per_nic = {}
            self._baseline(cur)

        # 今天已寫過的明細接回記憶體(重啟監控不丟今天前段),順手清掉過期的明細檔。
        self.sample_log.extend(read_traffic_log(today))
        _prune_traffic_logs()
        self.last_time = time.monotonic()

    def _save_state(self):
        data = {
            "date": self.date,
            "day_sent": self.day_sent,
            "day_recv": self.day_recv,
            "day_lan_excluded": self.day_lan_excluded,
            "day_apps": self.day_apps,
            "day_app_exes": self.day_app_exes,
            "day_hours": self.day_hours,
            "saved_at": datetime.now().strftime("%H:%M:%S"),
            "surges": list(self.surges),
            "per_nic": self.per_nic,
            "last_per_nic": self.last_per_nic,
        }
        write_json_atomic(STATE_PATH, data)

    def sample(self):
        cur = self._counters_fn()
        now = time.monotonic()
        today = self._today()

        ended = None
        with self.lock:
            if today != self.date:
                # 換日:昨天的累計先留一份,出鎖後結算進每日統計。
                ended = (self.date, self.day_recv, self.day_sent, self.day_lan_excluded,
                         self.day_apps, self.day_app_exes, self.day_hours)
                # Day rollover: zero the day's accumulators, keep last_per_nic
                # continuous so the rollover moment doesn't dump a huge delta.
                self.date = today
                self.day_sent = 0
                self.day_recv = 0
                self.day_lan_excluded = 0
                self.day_apps = {}
                self.day_app_exes = {}
                self.day_hours = {}
                self.sample_log.clear()
                self.surges.clear()
                self._surge_seq = 0
                self.per_nic = {}
                # 跨日把待消耗的內網/外網/暴增量歸零,免得昨天尾巴的 pending 扣到今天。
                self._pending_lan_sent = 0
                self._pending_lan_recv = 0
                self._pending_ext_sent = 0
                self._pending_ext_recv = 0
                self._pending_source_net = {}

            dt = max(1e-6, now - (self.last_time or now))
            step_sent = 0
            step_recv = 0
            for nic, st in cur.items():
                if self._excluded(nic):
                    continue
                prev = self.last_per_nic.get(nic)
                if prev is None:
                    # NIC appeared mid-run: baseline, don't count its history.
                    self.last_per_nic[nic] = [st.bytes_sent, st.bytes_recv]
                    continue
                ds = st.bytes_sent - prev[0]
                dr = st.bytes_recv - prev[1]
                if ds < 0:  # counter reset (reboot / NIC reset)
                    ds = st.bytes_sent
                if dr < 0:
                    dr = st.bytes_recv
                step_sent += ds
                step_recv += dr
                acc = self.per_nic.setdefault(nic, [0, 0])
                acc[0] += ds
                acc[1] += dr
                self.last_per_nic[nic] = [st.bytes_sent, st.bytes_recv]

            # 內網排除:按「內網 payload 占總 payload 的比例」分攤實際 NIC 增量後扣除。
            # ESTATS 給的是應用層 payload(不含 TCP/IP 標頭、重傳),直接拿來扣會漏掉線路上
            # 那 ~4% 標頭;改成「內網占比 × NIC 增量」就能連標頭一起按比例扣掉。內網占滿時
            # 幾乎扣光整個 NIC 增量;沒抓到任何連線(payload 全 0)時不扣(寧可算到、不漏算)。
            # 即時速率仍反映實際 NIC(供限速判斷);count_ranges 判為外網、照計。
            lan_sent = lan_recv = 0
            if self.exclude_lan_enabled:
                tot_recv = self._pending_lan_recv + self._pending_ext_recv
                tot_sent = self._pending_lan_sent + self._pending_ext_sent
                if tot_recv > 0:
                    lan_recv = min(step_recv,
                                   int(round(step_recv * self._pending_lan_recv / tot_recv)))
                if tot_sent > 0:
                    lan_sent = min(step_sent,
                                   int(round(step_sent * self._pending_lan_sent / tot_sent)))
                if self.debug_lan:
                    # 逐方向明細:NIC 實際增量 / ESTATS 內網 payload / 對外 payload / 本次實扣。
                    # 收方向實扣應≈NIC(純內網下載);送方向 payload≈0(全是 ACK)→ 實扣會偏低,
                    # 那段 ACK 就是大檔仍被計入的殘量。
                    print(f"[lan] recv: NIC={step_recv} lan_pay={self._pending_lan_recv} "
                          f"ext_pay={self._pending_ext_recv} excl={lan_recv}  |  "
                          f"sent: NIC={step_sent} lan_pay={self._pending_lan_sent} "
                          f"ext_pay={self._pending_ext_sent} excl={lan_sent}")
            counted_sent = step_sent - lan_sent
            counted_recv = step_recv - lan_recv
            sources = self._pending_source_net
            self._pending_source_net = {}
            gap_from = self._resume_gap_from
            self._resume_gap_from = None

            # 歸因:ESTATS 對到的外網 TCP 連線照實記到各程式;NIC 計入量減掉這些,剩下的(UDP/QUIC、
            # 標頭與 ACK、沒提權時的全部)記到「未歸因」,不再憑空消失。
            # 剛重啟的第一筆含「監控沒在跑」那段的網卡差額,整筆記到 OFFLINE,不歸給當下的程式。
            if gap_from:
                app_step = {OFFLINE: [counted_recv, counted_sent]}
                unattr_recv = unattr_sent = 0
            else:
                app_step = {}
                for name, r, sd, exe in sources.values():
                    acc = app_step.setdefault(name, [0, 0])
                    acc[0] += r
                    acc[1] += sd
                    self.day_app_exes.setdefault(name, exe)
                attr_recv = sum(v[0] for v in app_step.values())
                attr_sent = sum(v[1] for v in app_step.values())
                unattr_recv = max(0, counted_recv - attr_recv)
                unattr_sent = max(0, counted_sent - attr_sent)
                if unattr_recv or unattr_sent:
                    app_step[UNATTRIBUTED] = [unattr_recv, unattr_sent]
            for name, (r, sd) in app_step.items():
                acc = self.day_apps.setdefault(name, [0, 0])
                acc[0] += int(r)
                acc[1] += int(sd)
            # 逐時:重啟補進來的那筆不知道是哪個小時用的,不放進任何一小時。
            if not gap_from:
                daily_stats.add_hour(
                    self.day_hours, datetime.now().strftime("%H"), self.report_apps,
                    counted_recv, counted_sent,
                    [(self.day_app_exes.get(n, ""), r, sd) for n, (r, sd) in app_step.items()])

            top_sources = sorted(
                ({"name": name, "exe": exe, "pid": pid, "ip": raddr, "recv": int(r), "sent": int(sd)}
                 for (pid, raddr), (name, r, sd, exe) in sources.items() if r or sd),
                key=lambda x: x["recv"] + x["sent"], reverse=True)[:8]
            entry = {
                "t": datetime.now().strftime("%H:%M:%S"),
                "dt": round(dt, 1),
                "nic_recv": int(step_recv), "nic_sent": int(step_sent),
                "lan_pay_recv": int(self._pending_lan_recv), "ext_pay_recv": int(self._pending_ext_recv),
                "lan_pay_sent": int(self._pending_lan_sent), "ext_pay_sent": int(self._pending_ext_sent),
                "excl_recv": int(lan_recv), "excl_sent": int(lan_sent),
                "cnt_recv": int(counted_recv), "cnt_sent": int(counted_sent),
                "unattr_recv": int(unattr_recv), "unattr_sent": int(unattr_sent),
                "top": top_sources,
                "exlan": bool(self.exclude_lan_enabled), "estats": bool(self._estats_ok),
            }
            if gap_from:
                entry["gap_from"] = gap_from
            self._pending_lan_sent = self._pending_lan_recv = 0
            self._pending_ext_sent = self._pending_ext_recv = 0
            self.day_sent += counted_sent
            self.day_recv += counted_recv
            self.day_lan_excluded += lan_sent + lan_recv
            self.rate_up = step_sent / dt
            self.rate_down = step_recv / dt
            self.last_time = now

            # 記下本次下載速率,並丟掉視窗外的舊樣本(供 snapshot 取近期峰值)。
            self.rate_hist.append((now, self.rate_down))
            cutoff = now - self.peak_window_sec
            while self.rate_hist and self.rate_hist[0][0] < cutoff:
                self.rate_hist.popleft()

            self.last_sample_at = datetime.now()

            # 暴增偵測:用『實際計入額度』的量(已扣掉內網),不是網卡原始量 —— 否則內網
            # 下載(NAS/FTP,已排除)會狂報假暴增。重啟補進來的那一筆不是「這一分鐘」的量,不參與。
            if not gap_from:
                self._detect_surge(counted_sent + counted_recv, counted_sent, counted_recv,
                                   dt, sources, now, unattr_recv + unattr_sent)
            date = self.date

        # 以下不佔鎖:查 UDP 持有者、寫明細檔都是磁碟/系統呼叫,不該卡住 probe 與網頁輪詢。
        if ended:
            self._record_day(*ended)
        if unattr_recv + unattr_sent >= UDP_HINT_BYTES:
            entry["udp"] = _udp_owner_names()
        with self.lock:
            self.sample_log.append(entry)
        try:
            TRAFFIC_LOG_DIR.mkdir(parents=True, exist_ok=True)
            with traffic_log_path(date).open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            record_error("traffic_log", e)

        self._save_state()

    def probe_lan(self):
        """高頻探測(由 probe_loop 每 lan_probe_interval_sec 秒呼叫,預設 1s)。與 60s 的 NIC
        計費解耦:用 ESTATS 取每條 TCP 連線累積的收/發位元組,算出本探測區間的增量 →
        內網連線(對方 IP 落在 lan_ranges、且不在 count_ranges)的量累進 _pending_lan_*(供
        sample() 消耗),各 IP／程序來源的外網增量累進 _pending_source_net(供暴增歸因)。需提權才拿得到
        數字;沒提權或兩功能都關就直接跳過。ESTATS 呼叫不佔鎖,只在更新 pending 時短暫上鎖。"""
        if not self._estats_ok:
            return
        if not (self.surge_enabled or self.exclude_lan_enabled):
            self._estats_prev = {}
            return

        conns = estats_probe()    # {conn_key: (pid, raddr_ip, data_in, data_out)}

        lan_sent = lan_recv = 0
        ext_sent = ext_recv = 0
        source_net = {}           # (pid, 對方 IP) -> [recv, sent](本區間外網增量,供歸因)
        # 第一輪探測(剛啟動、或剛開啟排除把 _estats_prev 清空)時,所有連線都「看似新出現」,
        # 但其累計值可能含啟動前的歷史 → 這一輪只立基準、不灌歷史,避免一次性灌爆比例。
        first_probe = not self._estats_prev
        for key, (pid, raddr, din, dout) in conns.items():
            prev = self._estats_prev.get(key)
            if prev is None:
                if first_probe:
                    continue      # 第一輪:只立基準,下一輪起算
                # 運行中『新誕生』的連線:累計值就是它本區間的增量,直接起算 ——
                # 否則一檔一關的短 FTP 連線常在拿到基準前就結束,整筆漏排(被算進額度)。
                d_in, d_out = din, dout
            else:
                # 累計值理應單調遞增;若變小代表 4-tuple 被新連線重用 → 用新值當增量(從 0 起算)。
                d_in = din - prev[0] if din >= prev[0] else din
                d_out = dout - prev[1] if dout >= prev[1] else dout
            if d_in < 0:
                d_in = 0
            if d_out < 0:
                d_out = 0
            if d_in or d_out:
                kind = self._classify_ip(raddr)
                if kind == "local":
                    continue      # 本機連線:沒經過網卡,不計入、不歸因、不參與內網比例
                is_lan = kind == "lan"
                # 暴增定義的是「計入額度」的外網量，來源明細同樣排除內網連線。
                if not is_lan:
                    rec = source_net.setdefault((pid, raddr), [0, 0])
                    rec[0] += d_in
                    rec[1] += d_out
                if is_lan:
                    lan_recv += d_in
                    lan_sent += d_out
                else:
                    ext_recv += d_in
                    ext_sent += d_out

        self._estats_prev = {k: (v[2], v[3]) for k, v in conns.items()}
        names = {pid: self._proc_name(pid) for (pid, _) in source_net}
        with self.lock:
            self._pending_lan_sent += lan_sent
            self._pending_lan_recv += lan_recv
            self._pending_ext_sent += ext_sent
            self._pending_ext_recv += ext_recv
            for (pid, raddr), (r, sd) in source_net.items():
                label, exe = names[pid]
                rec = self._pending_source_net.setdefault((pid, raddr), [label, 0, 0, exe])
                rec[1] += r
                rec[2] += sd

    def _proc_name(self, pid) -> tuple:
        """pid → (軟體名, 檔名)(見 procinfo.describe),快取 10 分鐘 —— pid 會被新程序重用,
        不定期重查的話,舊軟體名會一直掛在新程序上。"""
        now = time.monotonic()
        hit = self._name_cache.get(pid)
        if hit is not None and now - hit[2] < 600:
            return hit[0], hit[1]
        if len(self._name_cache) > 4000:
            self._name_cache.clear()
        label, exe = describe_process(pid)
        self._name_cache[pid] = (label, exe, now)
        return label, exe

    def _classify_ip(self, ip) -> str:
        """對方位址分三類:"local"(本機 loopback,不經網卡)/ "lan"(內網,要排除)/ "ext"(計入額度)。
        判斷順序:本機 → count_ranges(強制計入,優先於 lan_ranges,確保走對外線路的
        網段永不被排除)→ lan_ranges → 其餘一律計入。"""
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return "ext"
        if getattr(addr, "ipv4_mapped", None):
            addr = addr.ipv4_mapped          # ::ffff:a.b.c.d 當成 IPv4 比對網段
        if any(addr in net for net in self.local_nets):
            return "local"
        if any(addr in net for net in self.count_nets):
            return "ext"
        if any(addr in net for net in self.lan_nets):
            return "lan"
        return "ext"

    def _attribute_surge(self, source_net, unattributed=0):
        """以本區間各 IP／程序來源的 ESTATS 網路增量排名，供暴增紀錄逐列顯示。
        抓不到連線的部分(UDP/QUIC 等)併成一列「未歸因」,排行才不會把小咖當成兇手。"""
        rows = [{"ip": raddr, "name": name, "exe": exe, "pid": pid, "bytes": int(r + sd),
                 "recv": int(r), "sent": int(sd)}
                for (pid, raddr), (name, r, sd, exe) in source_net.items() if r + sd > 0]
        if unattributed > 0:
            rows.append({"ip": "—", "name": UNATTRIBUTED, "pid": None,
                         "bytes": int(unattributed), "recv": None, "sent": None})
        rows.sort(key=lambda x: x["bytes"], reverse=True)
        return rows

    def _detect_surge(self, step_total, step_sent, step_recv, dt,
                      source_net, now, unattributed=0):
        """在鎖內呼叫。把本次取樣用量入回看視窗;達門檻即歸因並記事件。
        source_net = 本 NIC 區間各 IP／程序來源的 ESTATS 網路增量，供逐來源紀錄。
        門檻 = max(絕對門檻, 近期中位數×倍率);中位數會隨持續下載升高、加冷卻 → 不洗版。"""
        self.byte_hist.append(step_total)
        if not self.surge_enabled:
            return
        hist = list(self.byte_hist)[:-1]          # 不含本筆,當作基準母體
        if len(hist) < 5:                          # 樣本不足(剛啟動),先暖機
            return
        s = sorted(hist)
        n = len(s)
        median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
        threshold = max(self.surge_min_bytes, median * self.surge_factor)
        is_surge = step_total >= threshold
        cooling = (now - self._last_surge_at) < self.surge_cooldown
        if is_surge and not cooling:
            self._last_surge_at = now
            self._surge_seq += 1
            self.surges.append({
                "seq": self._surge_seq,
                "time": datetime.now().strftime("%H:%M:%S"),
                "bytes": int(step_total),
                "recv": int(step_recv),
                "sent": int(step_sent),
                "interval_sec": round(dt, 1),
                "baseline": int(median),
                "top": self._attribute_surge(source_net, unattributed),
            })

    def traffic_log(self, date: str) -> list:
        """某天的逐分鐘明細(舊到新)。今天直接給記憶體那份。"""
        with self.lock:
            if date == self.date:
                return list(self.sample_log)
        return read_traffic_log(date)

    def snapshot(self) -> dict:
        with self.lock:
            used = self.day_sent + self.day_recv
            remaining = max(0, self.quota_bytes - used)
            peak_down = max((r for _, r in self.rate_hist), default=0.0)
            return {
                "date": self.date,
                "used_bytes": used,
                "sent_bytes": self.day_sent,
                "recv_bytes": self.day_recv,
                "quota_bytes": self.quota_bytes,
                "quota_gb": self.cfg["quota_gb"],
                "remaining_bytes": remaining,
                "percent": (used / self.quota_bytes * 100) if self.quota_bytes else 0,
                "rate_up": self.rate_up,
                "rate_down": self.rate_down,
                "peak_down_window": peak_down,                 # 近 N 分最高下載速率(bytes/s)
                "peak_window_min": self.peak_window_sec / 60,
                "per_nic": {
                    k: {"sent": v[0], "recv": v[1]} for k, v in self.per_nic.items()
                },
                # 「最後更新」誠實反映上次成功取樣的時間,而非當下時間 —— 取樣若卡住,
                # 這個時間就不再前進,頁面也據此判斷資料是否停更。
                "updated_at": self.last_sample_at.strftime("%H:%M:%S")
                              if self.last_sample_at else "—",
                "data_age_sec": round((datetime.now() - self.last_sample_at).total_seconds(), 1)
                                if self.last_sample_at else None,
                # 超過 3 個取樣週期(至少 3 分鐘)沒成功取樣 = 疑似卡住。
                "data_stale": (
                    self.last_sample_at is not None
                    and (datetime.now() - self.last_sample_at).total_seconds()
                        > max(180.0, float(self.cfg.get("sample_interval_sec", 60)) * 3)
                ),
                # 流量暴增預警:開關、門檻、近期事件(新到舊)。
                "surge_enabled": self.surge_enabled,
                "surge_min_mb": round(self.surge_min_bytes / 1048576),
                "surges": list(self.surges)[::-1],
                # 逐分鐘明細整天的量太大,不隨每 5 秒的輪詢送;只給筆數與最後一筆時間,
                # 網頁看到變了才去 /api/traffic/log 拉整天。
                "log_count": len(self.sample_log),
                "log_last": self.sample_log[-1]["t"] if self.sample_log else None,
                # 今日各程式累計(計入額度的量),大到小。
                "day_apps": sorted(
                    ({"name": k, "exe": self.day_app_exes.get(k, ""), "recv": v[0], "sent": v[1]}
                     for k, v in self.day_apps.items()),
                    key=lambda x: x["recv"] + x["sent"], reverse=True),
                # 內網排除:開關 + 今日已排除(內網)位元組 + 是否提權(沒提權時 ESTATS
                # 拿不到 per-connection byte,內網排除/暴增歸因停用,頁面據此提醒)。
                "exclude_lan_enabled": self.exclude_lan_enabled,
                "lan_excluded_bytes": self.day_lan_excluded,
                "estats_ok": self._estats_ok,
            }


def run_speedtest(cfg: dict, floor_mbps=None) -> dict:
    """主動測速:從測速點抓一小段,量實際達到的下載速率(Mbps),判斷是否疑似被限速。

    抓到的位元組也會被流量監控算進當日用量(這是真實流量),故會「用掉一點點額度」。
    為了少耗流量:被限速時(速率低)在 timeout 內就讀到低速天花板即停;快線則秒下完
    speedtest_bytes 即停。計時從連線建立後才起算(排除 DNS/TLS 握手延遲)。
    """
    floor = float(floor_mbps) if floor_mbps is not None else float(cfg.get("speed_floor_mbps", 5.0))
    base = cfg.get("speedtest_url", "https://speed.cloudflare.com/__down")
    nbytes = int(cfg.get("speedtest_bytes", 8 * 1024 * 1024))
    timeout = float(cfg.get("speedtest_timeout", 6))
    url = base + ("&" if "?" in base else "?") + "bytes=" + str(nbytes)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "flow-check-speedtest"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            start = time.monotonic()           # 握手後才起算,量純傳輸速率
            got = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                got += len(chunk)
                if time.monotonic() - start >= timeout:
                    break
        elapsed = max(1e-3, time.monotonic() - start)
        mbps = got * 8 / elapsed / 1e6
        return {
            "ok": True,
            "mbps": round(mbps, 2),
            "bytes": got,
            "seconds": round(elapsed, 2),
            "floor_mbps": floor,
            "throttled": mbps < floor,
        }
    except Exception as e:
        return {"ok": False, "message": str(e), "floor_mbps": floor}


def sampler_loop(monitor: Monitor, interval: int):
    while True:
        try:
            monitor.sample()
        except Exception as e:
            record_error("sample", e)
        time.sleep(max(5, interval))


def probe_loop(monitor: Monitor, interval: int):
    """高頻 ESTATS 探測迴圈(與 60s NIC 計費解耦):每 interval 秒取一次各連線累計收/發 byte、
    算增量並累進 pending。間隔短,短而快的內網下載也能被探到兩次、算出增量並排除。"""
    while True:
        try:
            monitor.probe_lan()
        except Exception as e:
            record_error("probe_lan", e)
        time.sleep(max(1, interval))
