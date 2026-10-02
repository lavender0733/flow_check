"""
每日用量統計(長期保存,供流量報表)
====================================
逐分鐘明細只留 31 天,今日程式排行跨日就歸零 —— 要拿「每天 ssh 用掉多少」去跟公司反應,
得另外留一份不會過期的每日彙總。每天一筆:

  total         計入額度的總量 [收, 發](已扣內網,跟頁面上的「今日已用」同一個數)
  apps          指定幾支程式的量 [收, 發],依程序檔名歸類(預設 ssh / VS Code / Claude Code,
                config.json 的 report_apps 可改)。只算 ESTATS 對得到的外網 TCP 連線。
  unattributed  對不到程式的量(UDP/QUIC、標頭、沒提權時的全部)—— 這個大代表 apps 偏低
  offline       監控沒在跑那段期間補進來的量
  lan_excluded  已排除的內網量
  hours         逐時:{"10": {"total": [收, 發], "ssh": [...], ...}},只放有流量的小時、量為 0 的程式不放。
                看一天裡各程式何時在用(例如 ssh 跟 Claude Code 是不是同時段一起衝高)。
                不含「監控沒在跑」補進來的量 —— 那筆不知道是哪個小時用的。
  source        "state" = 換日時由當天累計結算

存在 data/machines/daily_stats_<機器>.json:各機一檔、一天只寫一次,不會讓 Drive 一直重傳。
"""

from common.jsonio import read_json, write_json_atomic
from core.paths import DAILY_STATS_PATH

# 報表類別 -> 程序檔名(不分大小寫)。VS Code 的 Remote-SSH 連線走的是它開的 ssh.exe,
# 所以 ssh 與 VS Code 分開算:ssh 是連回公司的通道,VS Code 是它自己的更新/延伸套件等。
REPORT_APPS = {
    "ssh": ["ssh.exe"],
    "vscode": ["code.exe"],
    "claude": ["claude.exe"],
}
# 跟 sampler 的同名常數一致(這裡不能 import sampler,會循環相依)。
_UNATTRIBUTED = "__unattributed__"
_OFFLINE = "__offline__"


def report_apps(cfg: dict) -> dict:
    apps = cfg.get("report_apps")
    if not isinstance(apps, dict) or not apps:
        apps = REPORT_APPS
    return {str(k): [str(e).lower() for e in v] for k, v in apps.items() if isinstance(v, list)}


def _category(exe: str, apps: dict):
    exe = (exe or "").lower()
    for cat, exes in apps.items():
        if exe in exes:
            return cat
    return None


def _add(acc: list, r, s):
    acc[0] += int(r or 0)
    acc[1] += int(s or 0)


def add_hour(hours: dict, hour: str, apps: dict, recv, sent, items):
    """把一次取樣的量累進某小時。items = [(程序檔名, 收, 發), ...]。"""
    b = hours.setdefault(hour, {})
    _add(b.setdefault("total", [0, 0]), recv, sent)
    for exe, r, s in items:
        cat = _category(exe, apps)
        if cat and (r or s):
            _add(b.setdefault(cat, [0, 0]), r, s)


def from_day_state(apps: dict, day_recv, day_sent, lan_excluded, day_apps: dict, day_app_exes: dict,
                   hours=None) -> dict:
    """由當天累計(Monitor 的欄位或 state 存檔)結算成一筆。"""
    rec = {
        "total": [int(day_recv), int(day_sent)],
        "apps": {cat: [0, 0] for cat in apps},
        "unattributed": [0, 0],
        "offline": [0, 0],
        "lan_excluded": int(lan_excluded),
        "source": "state",
        "hours": hours if isinstance(hours, dict) else {},
    }
    for name, v in (day_apps or {}).items():
        if not (isinstance(v, (list, tuple)) and len(v) == 2):
            continue
        if name == _UNATTRIBUTED:
            _add(rec["unattributed"], *v)
        elif name == _OFFLINE:
            _add(rec["offline"], *v)
        else:
            cat = _category((day_app_exes or {}).get(name, ""), apps)
            if cat:
                _add(rec["apps"][cat], *v)
    return rec


def load() -> dict:
    """{日期: 一筆統計}。讀不到回空 dict。
    早期版本會用逐分鐘明細回推「估算」的日子(source = "log"),報表只要實際紀錄,讀的時候就濾掉;
    下次換日寫檔時自然從檔案裡消失。"""
    data = read_json(DAILY_STATS_PATH, default={})
    days = data.get("days") if isinstance(data, dict) else None
    if not isinstance(days, dict):
        return {}
    return {d: r for d, r in days.items() if isinstance(r, dict) and r.get("source") != "log"}


def _save(days: dict):
    write_json_atomic(DAILY_STATS_PATH, {"days": dict(sorted(days.items()))})


def record_day(date: str, rec: dict):
    """寫入某天的結算。"""
    days = load()
    days[date] = rec
    _save(days)
