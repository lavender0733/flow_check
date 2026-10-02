"""
同步到同事版(TIPO flow check)
==============================
TIPO flow check 不再手動改,一律由這裡產生:依 tools/sync_tipo.json 的清單把流量相關的
程式複製過去,只覆蓋程式,**對方的 data/ 與 config.json 一律不碰**。

paths.py 的資料路徑依對應表(rewrite)換成 TIPO 版原本的位置 —— 同事的 state、prefs
放在 data/ 底下(不是 data/machines/),換錯了同事那邊會讀不到今天的用量、當成第一次使用。
所以每一條 rewrite 的 old 在檔案裡必須剛好出現一次,否則整個停下來(RewriteError)。

流程:build 到暫存資料夾 → 啟動驗證(8799 埠,讀得到 TIPO 格式的 state)→ 印出 plan。
預設只預覽;加 --apply 才寫進 target,寫之前再印一次 plan 並要求輸入 y。

用法:py -3.13 tools/sync_tipo.py [--apply] [--target 路徑]
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[1]
SPEC_PATH = Path(__file__).with_name("sync_tipo.json")

# 「程式範圍」:dst 裡落在這些地方、但不在這次 copy 清單裡的檔案,視為過時的程式,會被刪掉。
# 範圍外的東西(同事自己放的筆記等)一律不碰。
_SCOPE_DIRS = ("program", "web")
_SCOPE_ROOT_GLOBS = ("*.bat", "requirements.txt", "README.md")

# 啟動驗證時放進 TIPO 格式 state 的特定值,用來證明讀到的是 data/state_*.json。
PROBE_RECV = 123_456_789
PROBE_SENT = 987_654


class RewriteError(Exception):
    """rewrite 的 old 沒有剛好出現一次。"""

    def __init__(self, entry: dict, count: int):
        self.entry = entry
        self.count = count
        super().__init__(
            f"{entry['file']}:預期剛好出現 1 次,實際 {count} 次 —— {entry['old']!r}")


# ============================================================
# 清單
# ============================================================
# 路徑比對一律不分大小寫:Windows 與 Google Drive 的檔案系統不分大小寫,
# Data/ 就是 data/、Paths.py 就是 paths.py。
def _is_protected(rel: str, spec: dict) -> bool:
    """protect 的每一項:等於該路徑、在該資料夾底下,或檔名相同(desktop.ini 每一層都有)。"""
    rel = rel.casefold()
    name = rel.rsplit("/", 1)[-1]
    for p in spec.get("protect", []):
        p = p.casefold()
        if rel == p or rel.startswith(p + "/") or name == p:
            return True
    return False


def _in_scope(rel: str) -> bool:
    rel = rel.casefold()
    if "/" in rel:
        return rel.split("/", 1)[0] in _SCOPE_DIRS
    return any(fnmatch(rel, g.casefold()) for g in _SCOPE_ROOT_GLOBS)


def _copy_list(src: Path, spec: dict) -> list:
    """copy 的 glob 展開成相對路徑(排序、去重),再套 exclude。"""
    rels = set()
    for pattern in spec["copy"]:
        hits = [p for p in src.glob(pattern) if p.is_file()]
        if not hits:
            raise FileNotFoundError(f"copy 清單的 {pattern!r} 在 {src} 找不到任何檔案")
        rels.update(p.relative_to(src).as_posix() for p in hits)
    rels = {r for r in rels if not any(fnmatch(r, x) for x in spec.get("exclude", []))}
    return sorted(r for r in rels if "__pycache__" not in r.split("/"))


def _files(root: Path) -> list:
    if not root.exists():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


# ============================================================
# build / plan / apply
# ============================================================
def build(src: Path, spec: dict, out: Path) -> None:
    """把 copy 清單的檔案複製到 out(必須是空的或不存在),套用 rewrite 與 readme。"""
    src, out = Path(src), Path(out)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"build 的輸出資料夾必須是空的:{out}")
    rels = _copy_list(src, spec)
    for rel in rels:
        if _is_protected(rel, spec):
            raise ValueError(f"copy 清單包含受保護的路徑:{rel}")
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, out / rel)

    for entry in spec.get("rewrite", []):
        target = out / entry["file"]
        if entry["file"] not in rels:
            raise RewriteError(entry, 0)
        # 以位元組讀寫:old/new 都是單行,保留原檔的行尾與編碼。
        data = target.read_bytes()
        old, new = entry["old"].encode("utf-8"), entry["new"].encode("utf-8")
        n = data.count(old)
        if n != 1:
            raise RewriteError(entry, n)
        target.write_bytes(data.replace(old, new))

    readme = spec.get("readme")
    if readme:
        shutil.copy2(src / readme["from"], out / readme["to"])


def plan(src: Path, dst: Path, spec: dict) -> dict:
    """比對 build 結果(src)與 dst。protect 裡的路徑永遠不會出現在任何清單。"""
    src, dst = Path(src), Path(dst)
    staged = [r for r in _files(src) if not _is_protected(r, spec)]
    # 以不分大小寫的鍵對應 dst 實際的檔名:dst 的 Paths.py 就是 staged 的 paths.py,
    # 不能一邊寫進去、一邊又當成過時檔刪掉。
    existing = {r.casefold(): r for r in _files(dst)}
    out = {"add": [], "update": [], "remove": [], "same": []}
    for rel in staged:
        cur = existing.get(rel.casefold())
        if cur is None:
            out["add"].append(rel)
        elif (src / rel).read_bytes() == (dst / cur).read_bytes():
            out["same"].append(rel)
        else:
            out["update"].append(rel)
    staged_keys = {r.casefold() for r in staged}
    out["remove"] = sorted(r for k, r in existing.items()
                           if k not in staged_keys and _in_scope(r) and not _is_protected(r, spec))
    return out


def apply(staged: Path, dst: Path, p: dict) -> None:
    """依 plan 把 staged 的檔案寫進 dst,並刪掉 remove 清單的檔案。"""
    staged, dst = Path(staged), Path(dst)
    for rel in p["add"] + p["update"]:
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staged / rel, dst / rel)
    for rel in p["remove"]:
        (dst / rel).unlink()
    # 刪完留下的空資料夾(只在程式範圍內)一併收掉。
    for d in sorted({(dst / r).parent for r in p["remove"]}, key=lambda x: len(x.parts), reverse=True):
        while d != dst and d.is_dir() and not any(d.iterdir()) and _in_scope(d.relative_to(dst).as_posix() + "/x"):
            d.rmdir()
            d = d.parent


# ============================================================
# 啟動驗證
# ============================================================
_LAUNCHER = r"""
import sys
from pathlib import Path
sys.dont_write_bytecode = True
root, log_dir = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root / "program"))
import core.paths as P
import core.sampler as Sm
# 逐分鐘明細導到暫存資料夾(直接改模組常數,不動 LOCALAPPDATA)。
P.TRAFFIC_LOG_DIR = Sm.TRAFFIC_LOG_DIR = log_dir
import webbrowser
webbrowser.open = lambda *a, **k: None
import core.traffic_monitor as T
T.main()
"""


def source_version(src: Path) -> str:
    text = (Path(src) / "program" / "core" / "traffic_monitor.py").read_text(encoding="utf-8")
    m = re.search(r'^VERSION = "([^"]+)"', text, re.M)
    return m.group(1) if m else ""


def _machine_id(root: Path) -> str:
    spec = importlib.util.spec_from_file_location(
        "_sync_ident", root / "program" / "common" / "ident.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.machine_id()


def _get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def verify_startup(built: Path, src: Path, port: int = 8799, config: dict = None) -> dict:
    """把 build 結果複製到暫存資料夾、放一份 TIPO 舊格式的 state,以 port 啟動後檢查。

    config 省略時用 src 的 config.json。port 一律改成指定的值。
    回傳 {"ok", "used_bytes", "version", "config_status", "config", "errors", "log"}。
    """
    result = {"ok": False, "used_bytes": None, "version": None,
              "config_status": None, "config": None, "errors": [], "log": ""}
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))
    except OSError:
        result["errors"].append(f"連接埠 {port} 已被占用,無法驗證")
        return result

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        root = tmp / "app"
        shutil.copytree(built, root)
        cfg = dict(config) if config is not None else json.loads(
            (Path(src) / "config.json").read_text(encoding="utf-8"))
        cfg["port"] = port
        (root / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        data = root / "data"
        data.mkdir(exist_ok=True)
        state = {   # TIPO 3.3.0 的格式:沒有 day_hours,網卡清單留空(不觸發換電腦偵測)
            "date": datetime.now().strftime("%Y-%m-%d"),
            "day_sent": PROBE_SENT, "day_recv": PROBE_RECV, "day_lan_excluded": 0,
            "day_apps": {}, "day_app_exes": {}, "saved_at": "00:00:00", "surges": [],
            "per_nic": {}, "last_per_nic": {},
        }
        (data / f"state_{_machine_id(root)}.json").write_text(json.dumps(state), encoding="utf-8")

        log_path = tmp / "server.log"
        env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1")
        with open(log_path, "w", encoding="utf-8") as log:
            proc = subprocess.Popen([sys.executable, "-c", _LAUNCHER, str(root), str(tmp / "traffic")],
                                    stdout=log, stderr=subprocess.STDOUT, env=env)
            base = f"http://127.0.0.1:{port}"
            try:
                status = None
                for _ in range(60):
                    if proc.poll() is not None:
                        break
                    try:
                        status, body = _get(base + "/api/status")
                        break
                    except (urllib.error.URLError, ConnectionError):
                        time.sleep(0.5)
                if status != 200:
                    result["errors"].append(f"/api/status 回 {status}")
                else:
                    result["used_bytes"] = json.loads(body).get("used_bytes")
                    code, page = _get(base + "/")
                    m = re.search(r"系統版本 v([0-9][^<\s]*)", page.decode("utf-8", "replace"))
                    result["version"] = m.group(1) if m else None
                    code, body = _get(base + "/api/config")
                    result["config_status"] = code
                    if code == 200:
                        result["config"] = json.loads(body)
            finally:
                proc.kill()
                proc.wait()
        result["log"] = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]

    want = source_version(src)
    if result["used_bytes"] is None or result["used_bytes"] < PROBE_RECV + PROBE_SENT:
        result["errors"].append(
            f"used_bytes={result['used_bytes']},沒讀到 data/state_*.json 的 {PROBE_RECV + PROBE_SENT}")
    if result["version"] != want:
        result["errors"].append(f"首頁版號 {result['version']} ≠ flow_check 的 {want}")
    if result["config_status"] != 200:
        result["errors"].append(f"/api/config 回 {result['config_status']}")
    result["ok"] = not result["errors"]
    return result


# ============================================================
# 主程式
# ============================================================
def _print_plan(p: dict, dst: Path) -> None:
    print(f"\n目標:{dst}")
    for key, label in (("add", "新增"), ("update", "更新"), ("remove", "刪除")):
        print(f"  {label} {len(p[key])} 個")
        for rel in p[key]:
            print(f"    {rel}")
    print(f"  不變 {len(p['same'])} 個")
    print("  data/、config.json、desktop.ini 一律不碰。")


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="把流量監控同步到同事版(TIPO flow check)")
    ap.add_argument("--apply", action="store_true", help="真的寫入 target(預設只預覽)")
    ap.add_argument("--target", help="覆寫 sync_tipo.json 的 target")
    args = ap.parse_args(argv)

    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    dst = Path(args.target or spec["target"])
    if not dst.is_dir():
        print(f"[錯誤] 找不到目標資料夾:{dst}")
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "staged"
        try:
            build(REPO, spec, staged)
        except (RewriteError, FileNotFoundError, ValueError) as e:
            print(f"[錯誤] build 失敗,不寫入:{e}")
            return 1
        print(f"build 完成:{len(_files(staged))} 個檔案。啟動驗證中(8799 埠)…")
        v = verify_startup(staged, REPO, port=8799)
        if not v["ok"]:
            print("[錯誤] 啟動驗證失敗,不寫入:")
            for e in v["errors"]:
                print("  -", e)
            print(v["log"])
            return 1
        print(f"啟動驗證通過:v{v['version']},讀到 TIPO 格式的 state(used_bytes={v['used_bytes']})。")

        p = plan(staged, dst, spec)
        _print_plan(p, dst)
        if not args.apply:
            print("\n這是預覽,沒有寫入。要寫入請在命令列執行:同步到同事版.bat --apply")
            return 0

        print("\n即將寫入上面的變更。確定嗎?輸入 y 繼續:", end=" ", flush=True)
        if input().strip().lower() != "y":
            print("已取消,沒有寫入。")
            return 0
        apply(staged, dst, p)
        print("已寫入。")
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
