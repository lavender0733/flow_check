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
import threading
import time
import urllib.request
from datetime import datetime

import psutil

from common.jsonio import read_json, write_json_atomic
from core.estats import process_is_elevated, estats_probe
from core.health import record_error
from core.paths import DATA_DIR, STATE_PATH
from core.storage import load_prefs


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
        # 逐次取樣的計費明細(供網頁 log 框即時診斷:每筆 = 一次 NIC 結算怎麼分內/外網)。
        # 只保留近 5 分鐘(頁面只顯示 5 分鐘):依取樣間隔換算筆數,至少留 5 筆。
        self.sample_log = collections.deque(maxlen=max(5, (300 + interval - 1) // interval))

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
        # 強制計入白名單(優先於 lan_ranges):保護對外/額度流量(如 GPSS 10.65.x.x)。
        self.count_nets = []
        for c in cfg.get("count_ranges", []):
            try:
                self.count_nets.append(ipaddress.ip_network(c, strict=False))
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
        self._pending_ext_sent = 0    # 外網連線『上傳』payload(含 GPSS 10.65)
        self._pending_ext_recv = 0    # 外網連線『下載』payload
        # (pid, 對方 IP) -> [程序名, bytes]:自上次 NIC 取樣以來的外網來源增量,供暴增歸因與明細。
        self._pending_source_net = {}
        self._name_cache = {}         # pid -> 程序名(避免每輪都查 psutil)

        self._load_state()

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

        if saved and saved.get("date") == today and same_machine:
            # Resume today's tally across a restart.
            self.date = today
            self.day_sent = int(saved.get("day_sent", 0))
            self.day_recv = int(saved.get("day_recv", 0))
            self.day_lan_excluded = int(saved.get("day_lan_excluded", 0))
            # 舊版 state 沒有 surges 時自然從空白開始;新版本保存當日完整來源紀錄供頁面展開。
            saved_surges = saved.get("surges", [])
            if isinstance(saved_surges, list):
                self.surges.extend(ev for ev in saved_surges if isinstance(ev, dict))
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
            self.surges.clear()
            self._surge_seq = 0
            self.per_nic = {}
            self._baseline(cur)

        self.last_time = time.monotonic()

    def _save_state(self):
        data = {
            "date": self.date,
            "day_sent": self.day_sent,
            "day_recv": self.day_recv,
            "day_lan_excluded": self.day_lan_excluded,
            "surges": list(self.surges),
            "per_nic": self.per_nic,
            "last_per_nic": self.last_per_nic,
        }
        write_json_atomic(STATE_PATH, data)

    def sample(self):
        cur = self._counters_fn()
        now = time.monotonic()
        today = self._today()

        with self.lock:
            if today != self.date:
                # Day rollover: zero the day's accumulators, keep last_per_nic
                # continuous so the rollover moment doesn't dump a huge delta.
                self.date = today
                self.day_sent = 0
                self.day_recv = 0
                self.day_lan_excluded = 0
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
            # 即時速率仍反映實際 NIC(供限速判斷);GPSS 連 10.65.x 判為外網、照計。
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
            # 逐次計費明細(供網頁 log 框)：在 pending 歸零前快照本次結算怎麼分內/外網。
            self.sample_log.append({
                "t": datetime.now().strftime("%H:%M:%S"),
                "dt": round(dt, 1),
                "nic_recv": int(step_recv), "nic_sent": int(step_sent),
                "lan_pay_recv": int(self._pending_lan_recv), "ext_pay_recv": int(self._pending_ext_recv),
                "lan_pay_sent": int(self._pending_lan_sent), "ext_pay_sent": int(self._pending_ext_sent),
                "excl_recv": int(lan_recv), "excl_sent": int(lan_sent),
                "cnt_recv": int(step_recv - lan_recv), "cnt_sent": int(step_sent - lan_sent),
                "exlan": bool(self.exclude_lan_enabled), "estats": bool(self._estats_ok),
            })
            self._pending_lan_sent = self._pending_lan_recv = 0
            self._pending_ext_sent = self._pending_ext_recv = 0
            self.day_sent += step_sent - lan_sent
            self.day_recv += step_recv - lan_recv
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
            # 下載(NAS/FTP,已排除)會狂報假暴增「用了 X GB」,但其實沒吃額度。歸因用高頻
            # ESTATS 探測自上次 NIC 取樣以來累積的「各 IP／程序來源網路增量」
            # (_pending_source_net),消耗後歸零,下個 NIC 區間重新累積。
            counted_sent = step_sent - lan_sent
            counted_recv = step_recv - lan_recv
            self._detect_surge(counted_sent + counted_recv, counted_sent, counted_recv,
                               dt, self._pending_source_net, now)
            self._pending_source_net = {}

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
        source_net = {}           # (pid, 對方 IP) -> bytes(本區間外網增量,供暴增歸因)
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
                is_lan = self._is_lan_ip(raddr)
                # 暴增定義的是「計入額度」的外網量，來源明細同樣排除內網連線。
                if not is_lan:
                    source_key = (pid, raddr)
                    source_net[source_key] = source_net.get(source_key, 0) + d_in + d_out
                if is_lan:
                    lan_recv += d_in
                    lan_sent += d_out
                else:
                    ext_recv += d_in
                    ext_sent += d_out

        self._estats_prev = {k: (v[2], v[3]) for k, v in conns.items()}
        with self.lock:
            self._pending_lan_sent += lan_sent
            self._pending_lan_recv += lan_recv
            self._pending_ext_sent += ext_sent
            self._pending_ext_recv += ext_recv
            for (pid, raddr), b in source_net.items():
                rec = self._pending_source_net.setdefault(
                    (pid, raddr), [self._proc_name(pid), 0])
                rec[1] += b

    def _proc_name(self, pid) -> str:
        """pid → 程序名(快取;查不到回 pid 字串)。供暴增歸因顯示用。"""
        name = self._name_cache.get(pid)
        if name is None:
            try:
                name = psutil.Process(pid).name()
            except Exception:
                name = f"pid {pid}"
            self._name_cache[pid] = name
        return name

    def _is_lan_ip(self, ip) -> bool:
        """對方位址是否視為「內網、要排除」。count_ranges(強制計入)優先:落在其中一律
        當成『非內網』回 False,確保對外/額度流量(如 GPSS 10.65.x.x)永不被排除。"""
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if any(addr in net for net in self.count_nets):
            return False
        return any(addr in net for net in self.lan_nets)

    def _attribute_surge(self, source_net):
        """以本區間各 IP／程序來源的 ESTATS 網路增量排名，供暴增紀錄逐列顯示。"""
        deltas = sorted(
            ((b, name, pid, raddr)
             for (pid, raddr), (name, b) in source_net.items() if b > 0),
            reverse=True)
        return [{"ip": raddr, "name": name, "pid": pid, "bytes": int(b)}
                for b, name, pid, raddr in deltas]

    def _detect_surge(self, step_total, step_sent, step_recv, dt,
                      source_net, now):
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
                "top": self._attribute_surge(source_net),
            })

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
                # 逐次計費明細(新到舊),供網頁 log 框即時診斷內網排除。
                "sample_log": list(self.sample_log)[::-1],
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
