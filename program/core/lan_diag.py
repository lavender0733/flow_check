#!/usr/bin/env python3
"""獨立的「內網排除」診斷工具(不影響主程式、不需開 debug_lan)。

跟主程式共用『同一支 estats_probe』(core/estats.py)掃 TCP 連線(IPv4 + IPv6)的累計收/發位元組,
每 probe 秒算各連線增量,依 config.json 的 lan_ranges / count_ranges 分內/外網;同時讀
NIC 計數器算實際線路增量。每 report 秒印一份報表:

  ┌ 逐方向:NIC 實際增量 / 內網 payload / 對外 payload / 現行模型實扣 / 被計入(=漏算)
  └ 逐連線:對方 IP、分類(LOCAL/LAN/EXT/COUNT)、本段收/送 payload

並同時算出「雙向合併比例」會扣多少 —— 讓你當場比對:大檔下載的送方向(多半全是
沒有 payload 的 ACK)在現行模型漏算多少、改成合併比例能救回多少。

用法(須以系統管理員執行,ESTATS 啟用收集要提權):
    py -3 program/core/lan_diag.py [report_秒數]
    report_秒數 省略時預設 10。Ctrl-C 結束。
"""
import sys
import time
import json
import ipaddress
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True
# 把 program/ 放上 sys.path,讓 core / common 以套件匯入(同 traffic_monitor.py 的進入點寫法)。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psutil  # noqa: E402
from core.estats import estats_probe, process_is_elevated, _IS_WINDOWS  # noqa: E402
from core.storage import load_config  # noqa: E402


def _mb(n: float) -> str:
    return f"{n / (1024 * 1024):8.2f} MB"


def main():
    if not _IS_WINDOWS:
        print("此工具僅支援 Windows。")
        return
    if not process_is_elevated():
        print("[!] 非系統管理員身分 —— ESTATS 拿不到每條連線位元組,診斷無效。")
        print("    請以系統管理員開啟終端機後再執行。")
        return

    cfg = load_config()
    probe_int = max(1, int(cfg.get("lan_probe_interval_sec", 1)))
    report_int = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    exclude_nics = [x.lower() for x in cfg.get("exclude_nics", [])]

    lan_nets = []
    for c in cfg.get("lan_ranges", []):
        try:
            lan_nets.append(ipaddress.ip_network(c, strict=False))
        except ValueError:
            pass
    local_nets = []
    for c in cfg.get("local_ranges", ["127.0.0.0/8", "::1/128"]):
        try:
            local_nets.append(ipaddress.ip_network(c, strict=False))
        except ValueError:
            pass
    count_nets = []
    for c in cfg.get("count_ranges", []):
        try:
            count_nets.append(ipaddress.ip_network(c, strict=False))
        except ValueError:
            pass

    def classify(ip: str) -> str:
        """回傳 'LOCAL'(本機,不經網卡)/ 'COUNT'(強制計入)/ 'LAN'(內網排除)/ 'EXT'(對外計入)。"""
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return "EXT"
        if any(addr in n for n in local_nets):
            return "LOCAL"
        if any(addr in n for n in count_nets):
            return "COUNT"
        if any(addr in n for n in lan_nets):
            return "LAN"
        return "EXT"

    def nic_totals():
        cur = psutil.net_io_counters(pernic=True)
        sent = recv = 0
        for nic, st in cur.items():
            if any(x in nic.lower() for x in exclude_nics):
                continue
            sent += st.bytes_sent
            recv += st.bytes_recv
        return sent, recv

    port = int(cfg.get("port", 8765))
    status_url = f"http://127.0.0.1:{port}/api/status"

    def monitor_counts():
        """抓正在跑的監控程式『今日已計入』的收/送/已排除位元組(抓不到回 None)。"""
        try:
            with urllib.request.urlopen(status_url, timeout=2) as r:
                d = json.loads(r.read().decode("utf-8"))
            return d.get("recv_bytes", 0), d.get("sent_bytes", 0), d.get("lan_excluded_bytes", 0)
        except Exception:
            return None

    print(f"內網段 lan_ranges = {[str(n) for n in lan_nets]}")
    print(f"強制計入 count_ranges = {[str(n) for n in count_nets]}")
    print(f"本機 local_ranges = {[str(n) for n in local_nets]}(不經網卡,不計入)")
    print(f"探測 {probe_int}s / 報表 {report_int:g}s。Ctrl-C 結束。")
    mon0 = monitor_counts()
    if mon0 is None:
        print(f"[note] 連不到監控 API ({status_url}) —— 只顯示診斷自身估算,不對照監控實記。\n")
    else:
        print(f"[ok] 已連上監控 API,將並排顯示『監控實際計入』增量。\n")
    mon_prev = mon0
    mon_start = mon0                 # 開始時的監控計數,供「自開始累計」對照

    estats_prev = {}                 # key -> (din, dout)
    # 本報表區間累計
    lan_recv = lan_sent = ext_recv = ext_sent = 0
    per_host = {}                    # ip -> [recv_payload, sent_payload, kind]
    # 自開始的累計(用來跨多次 60s 結算、判斷監控到底有沒有把內網算進額度)
    cum_nic_recv = cum_nic_sent = 0
    cum_lan_recv = cum_lan_sent = 0
    base_sent, base_recv = nic_totals()
    next_report = time.monotonic() + report_int
    name_cache = {}

    def proc_name(pid):
        nm = name_cache.get(pid)
        if nm is None:
            try:
                nm = psutil.Process(pid).name()
            except Exception:
                nm = f"pid {pid}"
            name_cache[pid] = nm
        return nm

    try:
        while True:
            time.sleep(probe_int)
            conns = estats_probe()
            first_probe = not estats_prev
            for key, (pid, raddr, din, dout) in conns.items():
                prev = estats_prev.get(key)
                if prev is None:
                    if first_probe:
                        continue
                    d_in, d_out = din, dout          # 運行中新連線:累計即增量
                else:
                    d_in = din - prev[0] if din >= prev[0] else din
                    d_out = dout - prev[1] if dout >= prev[1] else dout
                if d_in < 0:
                    d_in = 0
                if d_out < 0:
                    d_out = 0
                if not (d_in or d_out):
                    continue
                kind = classify(raddr)
                if kind == "LOCAL":
                    # 本機連線不經網卡,主程式整筆略過;這裡照樣列在逐連線表供對照,但不進比例。
                    rec = per_host.setdefault(f"{raddr} [{proc_name(pid)}]", [0, 0, kind])
                    rec[0] += d_in
                    rec[1] += d_out
                    continue
                if kind == "LAN":
                    lan_recv += d_in
                    lan_sent += d_out
                else:                                # EXT 與 COUNT 都計入額度
                    ext_recv += d_in
                    ext_sent += d_out
                label = f"{raddr} [{proc_name(pid)}]"
                rec = per_host.setdefault(label, [0, 0, kind])
                rec[0] += d_in
                rec[1] += d_out
            estats_prev = {k: (v[2], v[3]) for k, v in conns.items()}

            if time.monotonic() < next_report:
                continue

            # ── 出報表 ──
            cur_sent, cur_recv = nic_totals()
            step_sent = max(0, cur_sent - base_sent)
            step_recv = max(0, cur_recv - base_recv)
            base_sent, base_recv = cur_sent, cur_recv
            cum_nic_recv += step_recv
            cum_nic_sent += step_sent
            cum_lan_recv += lan_recv
            cum_lan_sent += lan_sent

            tot_recv = lan_recv + ext_recv
            tot_sent = lan_sent + ext_sent
            # 現行模型:逐方向各自按 payload 比例分攤 NIC 增量。
            cur_excl_recv = min(step_recv, round(step_recv * lan_recv / tot_recv)) if tot_recv else 0
            cur_excl_sent = min(step_sent, round(step_sent * lan_sent / tot_sent)) if tot_sent else 0
            # 提案:雙向合併單一比例,同時套用到收與送(讓下載把送方向的 ACK 也一起扣掉)。
            tot_all = tot_recv + tot_sent
            comb = (lan_recv + lan_sent) / tot_all if tot_all else 0
            alt_excl_recv = min(step_recv, round(step_recv * comb))
            alt_excl_sent = min(step_sent, round(step_sent * comb))

            print("=" * 78)
            print(f"{time.strftime('%H:%M:%S')}  (此區間約 {report_int:g}s)")
            print(f"  收 recv: NIC={_mb(step_recv)}  內網payload={_mb(lan_recv)}  對外payload={_mb(ext_recv)}")
            print(f"           現行實扣={_mb(cur_excl_recv)}  →被計入={_mb(step_recv - cur_excl_recv)}"
                  f"   |  合併比例實扣={_mb(alt_excl_recv)}")
            print(f"  送 sent: NIC={_mb(step_sent)}  內網payload={_mb(lan_sent)}  對外payload={_mb(ext_sent)}")
            print(f"           現行實扣={_mb(cur_excl_sent)}  →被計入={_mb(step_sent - cur_excl_sent)}"
                  f"   |  合併比例實扣={_mb(alt_excl_sent)}")
            leak_now = (step_recv - cur_excl_recv) + (step_sent - cur_excl_sent)
            leak_alt = (step_recv - alt_excl_recv) + (step_sent - alt_excl_sent)
            print(f"  合計被計入(診斷估算): 現行={_mb(leak_now)}   合併比例={_mb(leak_alt)}")

            # ── 對照「實際在跑的監控」本區間真的計入了多少 ──
            mon_now = monitor_counts()
            if mon_now is not None and mon_prev is not None:
                d_recv = max(0, mon_now[0] - mon_prev[0])
                d_sent = max(0, mon_now[1] - mon_prev[1])
                d_excl = max(0, mon_now[2] - mon_prev[2])
                print(f"  ★ 監控實記本區間: 計入 recv={_mb(d_recv)} sent={_mb(d_sent)}"
                      f"   已排除={_mb(d_excl)}")
            if mon_now is not None:
                mon_prev = mon_now

            # ── 自開始的累計對照(跨多次 60s 結算才看得準)──
            print(f"  ── 自開始累計 ──")
            print(f"     NIC 收={_mb(cum_nic_recv)} 送={_mb(cum_nic_sent)};"
                  f" 其中內網payload 收={_mb(cum_lan_recv)} 送={_mb(cum_lan_sent)}")
            if mon_start is not None and mon_now is not None:
                c_recv = max(0, mon_now[0] - mon_start[0])
                c_sent = max(0, mon_now[1] - mon_start[1])
                c_excl = max(0, mon_now[2] - mon_start[2])
                print(f"  ★★ 監控自開始: 計入額度 收={_mb(c_recv)} 送={_mb(c_sent)}  已排除={_mb(c_excl)}")
                print(f"      ↳ 判讀:若『計入額度 收』接近 NIC 收(而內網payload也很大)"
                      f"=監控把內網算進額度了(真漏算);若接近 0 =內網有被正確排除。")

            if per_host:
                print("  ── 逐連線(本區間 payload,依量排序)──")
                rows = sorted(per_host.items(), key=lambda kv: kv[1][0] + kv[1][1], reverse=True)
                for label, (r, s, kind) in rows[:12]:
                    print(f"    [{kind:5}] {label:42} 收={_mb(r)} 送={_mb(s)}")

            lan_recv = lan_sent = ext_recv = ext_sent = 0
            per_host = {}
            next_report = time.monotonic() + report_int
    except KeyboardInterrupt:
        print("\n結束。")


if __name__ == "__main__":
    main()
