"""
Windows ESTATS:每條 TCP 連線的精準收/發位元組
================================================
從 traffic_monitor.py 抽出的獨立底層模組(零專案內相依)。對外只暴露兩個函式:
process_is_elevated() 與 estats_probe();其餘為內部 ctypes 結構與輔助函式。

為什麼需要這個:實測這台 Windows 不把 socket 流量算進「每支程序的 io_counters」——
一筆 557MB 的 SFTP 下載,在程序的 read/write/other 全都看不到(只 ~60KB)。所以舊的
「用 io_counters 增量估程序網路量」根本拿不到網路數字,內網傳輸無從扣除。
改用 IP Helper 的 ESTATS:GetPerTcpConnectionEStats(TcpConnectionEstatsData) 直接取每條
TCP 連線自啟用收集以來累積的應用層 DataBytesIn/Out。再按對方 IP 判內/外網,精準扣掉內網。
限制:① 需以系統管理員身分啟動(SetPerTcpConnectionEStats 啟用收集是特權操作,非提權回
ACCESS_DENIED);② 只看得到 TCP。UDP(含 QUIC —— Chrome/YouTube/Meet 大量走它)沒有逐連線計數器,
抓不到程式名,sampler 會把這部分列成「未歸因」而不是默默漏掉。
IPv4 與 IPv6 都收:實測 Google Drive、Mattermost 在這台是走 IPv6 連線,只看 IPv4 時它們的
流量全都歸不到程式,暴增紀錄也看不到它們。
"""

import os
import socket
import struct
import ctypes
from ctypes import wintypes

_IS_WINDOWS = (os.name == "nt")
_MIB_TCP_STATE_ESTAB = 5
_TCP_ESTATS_DATA = 1                 # TCP_ESTATS_TYPE.TcpConnectionEstatsData

if _IS_WINDOWS:
    _iphlpapi = ctypes.WinDLL("iphlpapi.dll")


class _MIB_TCPROW2(ctypes.Structure):
    _fields_ = [("dwState", wintypes.DWORD), ("dwLocalAddr", wintypes.DWORD),
                ("dwLocalPort", wintypes.DWORD), ("dwRemoteAddr", wintypes.DWORD),
                ("dwRemotePort", wintypes.DWORD), ("dwOwningPid", wintypes.DWORD),
                ("dwOffloadState", wintypes.DWORD)]


class _MIB_TCP6ROW2(ctypes.Structure):
    _fields_ = [("LocalAddr", ctypes.c_ubyte * 16), ("dwLocalScopeId", wintypes.DWORD),
                ("dwLocalPort", wintypes.DWORD), ("RemoteAddr", ctypes.c_ubyte * 16),
                ("dwRemoteScopeId", wintypes.DWORD), ("dwRemotePort", wintypes.DWORD),
                ("State", wintypes.DWORD), ("dwOwningPid", wintypes.DWORD),
                ("dwOffloadState", wintypes.DWORD)]


class _MIB_TCP6ROW(ctypes.Structure):
    _fields_ = [("State", wintypes.DWORD), ("LocalAddr", ctypes.c_ubyte * 16),
                ("dwLocalScopeId", wintypes.DWORD), ("dwLocalPort", wintypes.DWORD),
                ("RemoteAddr", ctypes.c_ubyte * 16), ("dwRemoteScopeId", wintypes.DWORD),
                ("dwRemotePort", wintypes.DWORD)]


class _MIB_TCPROW(ctypes.Structure):
    _fields_ = [("dwState", wintypes.DWORD), ("dwLocalAddr", wintypes.DWORD),
                ("dwLocalPort", wintypes.DWORD), ("dwRemoteAddr", wintypes.DWORD),
                ("dwRemotePort", wintypes.DWORD)]


class _TCP_ESTATS_DATA_ROD_v0(ctypes.Structure):
    # 唯讀動態統計;我們只用 DataBytesIn / DataBytesOut(應用層 payload 累計位元組)。
    _fields_ = [("DataBytesOut", ctypes.c_ulonglong), ("DataSegsOut", ctypes.c_ulonglong),
                ("DataBytesIn", ctypes.c_ulonglong), ("DataSegsIn", ctypes.c_ulonglong),
                ("SegsOut", ctypes.c_ulonglong), ("SegsIn", ctypes.c_ulonglong),
                ("SoftErrors", wintypes.ULONG), ("SoftErrorReason", wintypes.ULONG),
                ("SndUna", wintypes.ULONG), ("SndNxt", wintypes.ULONG),
                ("SndMax", wintypes.ULONG), ("ThruBytesAcked", ctypes.c_ulonglong),
                ("RcvNxt", wintypes.ULONG), ("ThruBytesReceived", ctypes.c_ulonglong)]


class _TCP_ESTATS_DATA_RW_v0(ctypes.Structure):
    _fields_ = [("EnableCollection", ctypes.c_ubyte)]


def process_is_elevated() -> bool:
    """目前程序是否以系統管理員身分執行(ESTATS 啟用收集需要這個)。"""
    if not _IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _get_table(fn, row_cls):
    """列舉 TCP 連線表(GetTcpTable2 / GetTcp6Table2,含 owning PID)。回傳 row_cls list。"""
    size = wintypes.DWORD(0)
    fn(None, ctypes.byref(size), True)
    buf = (ctypes.c_byte * size.value)()
    ret = fn(buf, ctypes.byref(size), True)
    if ret != 0:
        raise OSError(f"{fn.__name__} failed: {ret}")
    n = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
    rows_addr = ctypes.addressof(buf) + ctypes.sizeof(wintypes.DWORD)
    return list((row_cls * n).from_address(rows_addr))


def _read_data_stats(set_fn, get_fn, row) -> "_TCP_ESTATS_DATA_ROD_v0 | None":
    """對一條連線啟用 Data 統計收集並讀回累積值;啟用或讀取失敗回 None。"""
    rw = _TCP_ESTATS_DATA_RW_v0(EnableCollection=1)
    en = set_fn(ctypes.byref(row), _TCP_ESTATS_DATA,
                ctypes.byref(rw), 0, ctypes.sizeof(rw), 0)
    if en != 0:
        return None             # 啟用失敗(多半沒提權)→ 此連線數字不可信,跳過
    rod = _TCP_ESTATS_DATA_ROD_v0()
    ret = get_fn(ctypes.byref(row), _TCP_ESTATS_DATA,
                 None, 0, 0, None, 0, 0,
                 ctypes.byref(rod), 0, ctypes.sizeof(rod))
    return rod if ret == 0 else None


def _v6_to_str(raw) -> str:
    """IPv6 位址轉字串;IPv4-mapped(::ffff:a.b.c.d)轉回 IPv4 寫法,內網網段比對才對得上。"""
    b = bytes(raw)
    if b[:12] == bytes(10) + bytes((0xFF, 0xFF)):
        return socket.inet_ntoa(b[12:])
    return socket.inet_ntop(socket.AF_INET6, b)


def estats_probe():
    """回傳 {conn_key: (pid, raddr_ip, data_in, data_out)} —— 每條已建立的 TCP 連線(IPv4 +
    IPv6)自啟用收集以來累積的應用層收/發位元組。只收『啟用收集成功(en==0)』的連線
    (沒提權時 SetPerTcp*ConnectionEStats 回 ACCESS_DENIED=5,且讀回的 ROD 是垃圾值,一律不收)。
    全程 best-effort,失敗回空 dict;IPv6 那半失敗不影響 IPv4。"""
    if not _IS_WINDOWS:
        return {}
    conns = {}
    try:
        rows = _get_table(_iphlpapi.GetTcpTable2, _MIB_TCPROW2)
    except OSError:
        rows = []
    for r in rows:
        if r.dwState != _MIB_TCP_STATE_ESTAB:
            continue
        row = _MIB_TCPROW(r.dwState, r.dwLocalAddr, r.dwLocalPort,
                          r.dwRemoteAddr, r.dwRemotePort)
        rod = _read_data_stats(_iphlpapi.SetPerTcpConnectionEStats,
                               _iphlpapi.GetPerTcpConnectionEStats, row)
        if rod is None:
            continue
        raddr = socket.inet_ntoa(struct.pack("<I", r.dwRemoteAddr))
        key = (r.dwLocalAddr, r.dwLocalPort, r.dwRemoteAddr, r.dwRemotePort)
        conns[key] = (r.dwOwningPid, raddr, rod.DataBytesIn, rod.DataBytesOut)

    try:
        rows6 = _get_table(_iphlpapi.GetTcp6Table2, _MIB_TCP6ROW2)
    except OSError:
        rows6 = []
    for r in rows6:
        if r.State != _MIB_TCP_STATE_ESTAB:
            continue
        row = _MIB_TCP6ROW(r.State, r.LocalAddr, r.dwLocalScopeId, r.dwLocalPort,
                           r.RemoteAddr, r.dwRemoteScopeId, r.dwRemotePort)
        rod = _read_data_stats(_iphlpapi.SetPerTcp6ConnectionEStats,
                               _iphlpapi.GetPerTcp6ConnectionEStats, row)
        if rod is None:
            continue
        # 鍵與 IPv4 的不同形(開頭多一個 6),兩邊不會撞在一起。
        key = (6, bytes(r.LocalAddr), r.dwLocalPort, bytes(r.RemoteAddr), r.dwRemotePort)
        conns[key] = (r.dwOwningPid, _v6_to_str(r.RemoteAddr), rod.DataBytesIn, rod.DataBytesOut)
    return conns
