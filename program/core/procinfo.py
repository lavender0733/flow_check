"""
程序 → 使用者看得懂的軟體名稱
================================
流量歸因原本只顯示程序檔名(GoogleDriveFS.exe、svchost.exe…),一般人看不出是哪個軟體。
這裡把 pid 換成「軟體名稱」,供今日程式排行、用量明細、暴增紀錄顯示:

  1. 本工具自己的程序(流量監控本身,690 抓信 / KM / s690 / 測速都在這支 python 裡)→ 直接標明
     「本工具」,免得把自己用掉的量誤認成別的軟體。只認命令列,不認檔名:別的程式開的同名程序
     (例如 Playwright 的 chrome-headless-shell.exe)不能算成本工具。
  2. svchost.exe:一個檔名底下跑著各式 Windows 服務,檔名毫無資訊 → 反查該 pid 承載的服務顯示名
     (例如「Windows Update」「傳遞最佳化」)。
  3. 其餘:讀 exe 內嵌的版本資訊(檔案內容 → 詳細資料看到的那個)的 FileDescription,
     沒有就用 ProductName;都沒有才退回檔名。實測 GoogleDriveFS.exe → Google Drive、
     Mattermost.exe → Mattermost、chrome.exe → Google Chrome。

零專案內相依的葉模組(只用 psutil 與 ctypes)。全程 best-effort,查不到一律退回檔名,絕不拋例外。
查一次約數毫秒,呼叫端(sampler)依 pid 快取,所以只有新出現的程序才會查。
"""

import os
import ctypes
from ctypes import wintypes

import psutil

_IS_WINDOWS = (os.name == "nt")

SELF_MONITOR = "流量監控(本工具・測速)"

# 這些 ProductName 是作業系統共用的,拿來當軟體名等於沒說 → 改用 FileDescription。
_GENERIC_PRODUCTS = ("operating system", "作業系統")

if _IS_WINDOWS:
    _ver = ctypes.WinDLL("version.dll")
    _ver.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    _ver.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    _ver.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    _ver.GetFileVersionInfoW.restype = wintypes.BOOL
    _ver.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    _ver.VerQueryValueW.restype = wintypes.BOOL


def _version_strings(path: str) -> dict:
    """讀 exe 版本資訊裡的 FileDescription / ProductName。讀不到回空 dict。"""
    if not _IS_WINDOWS or not path:
        return {}
    size = _ver.GetFileVersionInfoSizeW(path, None)
    if not size:
        return {}
    buf = ctypes.create_string_buffer(size)
    if not _ver.GetFileVersionInfoW(path, 0, size, buf):
        return {}
    p = ctypes.c_void_p()
    n = wintypes.UINT()
    langs = []
    # 先用檔案自己宣告的語系/字碼頁,再補幾個常見的(有些檔宣告了卻沒放對應字串表)。
    if _ver.VerQueryValueW(buf, r"\VarFileInfo\Translation", ctypes.byref(p), ctypes.byref(n)) and n.value:
        arr = ctypes.cast(p, ctypes.POINTER(wintypes.WORD * (n.value // 2))).contents
        langs = [(arr[i], arr[i + 1]) for i in range(0, len(arr) - 1, 2)]
    langs += [(0x0409, 0x04B0), (0x0409, 0x04E4), (0x0404, 0x04B0), (0x0000, 0x04B0)]
    out = {}
    for lang, cp in langs:
        for key in ("FileDescription", "ProductName"):
            if key in out:
                continue
            q = rf"\StringFileInfo\{lang:04x}{cp:04x}\{key}"
            if _ver.VerQueryValueW(buf, q, ctypes.byref(p), ctypes.byref(n)) and n.value:
                v = ctypes.wstring_at(p.value, n.value).rstrip("\0").strip()
                if v:
                    out[key] = v
        if len(out) == 2:
            break
    return out


def _clean(s: str) -> str:
    return " ".join(s.replace("®", "").replace("™", "").replace("(R)", "").split())


def _service_names(pid: int) -> list:
    """這個 pid(svchost)承載的 Windows 服務顯示名。"""
    names = []
    try:
        for svc in psutil.win_service_iter():
            try:
                if svc.pid() == pid:
                    names.append(svc.display_name())
            except Exception:
                continue
    except Exception:
        pass
    return names


def _self_tool_label(proc) -> str:
    """本工具自己的程序回對應標籤,否則回空字串。本工具不開子程序(8.0.0 起),
    所有連線都出在 traffic_monitor.py 那支 python 上,只看它自己的命令列。"""
    try:
        cmd = " ".join(proc.cmdline()).lower()
    except Exception:
        return ""
    return SELF_MONITOR if "traffic_monitor.py" in cmd else ""


def describe(pid: int) -> tuple:
    """pid → (軟體名稱, 程序檔名)。查不到的部分退回檔名或「pid N」。"""
    try:
        proc = psutil.Process(pid)
        exe_name = proc.name()
    except Exception:
        return (f"pid {pid}", f"pid {pid}")

    own = _self_tool_label(proc)
    if own:
        return (own, exe_name)

    if exe_name.lower() == "svchost.exe":
        svcs = _service_names(pid)
        if svcs:
            label = "Windows 服務:" + "、".join(svcs[:2]) + ("…" if len(svcs) > 2 else "")
            return (label, exe_name)
        return ("Windows 服務(svchost)", exe_name)

    try:
        info = _version_strings(proc.exe())
    except Exception:
        info = {}
    desc = _clean(info.get("FileDescription", ""))
    prod = _clean(info.get("ProductName", ""))
    if prod and any(g in prod.lower() for g in _GENERIC_PRODUCTS):
        prod = ""
    # FileDescription 通常就是軟體名(Google Drive、Mattermost、Microsoft Teams),而且比
    # ProductName 具體(Office 各元件的 ProductName 全是 Microsoft Office)。太長的描述多半是
    # 一句說明文字而不是名字,那種情況改用 ProductName。
    label = desc if desc and (len(desc) <= 40 or not prod) else (prod or desc)
    return (label or exe_name, exe_name)


def describe_exe_name(exe_name: str) -> str:
    """只有檔名(沒有 pid,例如升級前存下的暴增紀錄)時,盡量換成軟體名稱:
    找一支同檔名、還在跑的程序讀它的版本資訊;找不到就原樣回傳檔名。"""
    if not exe_name:
        return exe_name
    # svchost 每一支承載的服務都不同,拿現在隨便一支 svchost 的服務名來套會張冠李戴。
    if exe_name.lower() == "svchost.exe":
        return "Windows 服務(svchost)"
    try:
        for proc in psutil.process_iter(["name"]):
            if (proc.info.get("name") or "").lower() == exe_name.lower():
                label, _ = describe(proc.pid)
                if not label.startswith("pid "):
                    return label
                break
    except Exception:
        pass
    return exe_name
