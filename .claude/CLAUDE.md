# CLAUDE.md

## 命名與工作流程

- 叫「**TIPO flow check**」，不要叫「同事版」；介面與說明叫「**KM**」，現有 `wiki` 模組、設定鍵與 API 路徑不因此改名。
- 只叫你「閱讀」計畫或文件時，先不要動手，看完先確認待決事項。
- 主分支是 `master`；feat 分支等全部完成後才合併回 `master`。
- commit 只在我說了才做，訊息格式：`type(scope): 中文說明`，scope 用 `core`、`xfer690`、`docs` 等。
- 寫入 KM 有新的寫法時，先用最容易復原的方式試（例如只新增一行），確認沒問題再擴大範圍。

## 架構與模組職責

Windows 本機工具，Python 3.13 + psutil；690 另用 google-auth-oauthlib。前端是免打包、無框架的 HTML / CSS / JavaScript。

- `program/common/` 是共用底層，不回頭相依 `core` 或 `xfer690`。
- `program/core/` 是流量監控；下層模組不可回頭匯入組裝入口 `traffic_monitor.py`，避免循環匯入。
- `program/xfer690/` 是選用功能，可用 `common` 與 `core.paths`。入口的 `_start_xfer690()` 在函式內選用匯入；`core` 的其他模組不能在模組層級匯入 `xfer690`。整個套件不存在或載入失敗時，流量監控仍須能啟動（TIPO 沒有這個套件）。

| 模組 | 職責 |
| --- | --- |
| `core/traffic_monitor.py` | 組裝入口：載設定、建立 Monitor 與選用的 690 服務、啟動取樣／探測執行緒、開 HTTP 伺服器 |
| `core/routes.py` | `App`、HTTP 端點、`build_routes()` 註冊表、`render_index()` 首頁組版 |
| `core/sampler.py` | `Monitor` 流量取樣與計費、ESTATS 高頻探測、暴增歸因、逐分鐘明細、主動測速 |
| `core/daily_stats.py` | 每日與逐時用量統計、跨日保存 |
| `core/estats.py` | Windows ESTATS：IPv4／IPv6 逐 TCP 連線收發位元組、管理員身分檢查 |
| `core/procinfo.py` | 程序檔名 → 軟體／Windows 服務名稱 |
| `core/health.py` | 錯誤收集、掃描 core／common／xfer690 的 Python 檔與主設定，偵測需重啟 |
| `core/paths.py` | 專案路徑、機器代號、資料檔路徑常數 |
| `core/storage.py` | 主設定、本機偏好、機器友善名稱；偏好寫入有鎖與原子寫入 |
| `core/lan_diag.py` | `diag_lan.bat` 啟動的獨立內網排除診斷 |
| `common/webapp.py` | HTTP 分派、靜態檔、路徑穿越防護、例外處理、`serve()` |
| `common/jsonio.py` | JSON 容錯讀取、原子寫入、檔案簽章 |
| `common/ident.py` | 機器代號 |
| `xfer690/periods.py` | 期數 ↔ 日期 |
| `xfer690/parser.py` | 通知信主旨 → 事件；原始信 → log 摘要 |
| `xfer690/status.py` | 執行分段、資料日期／期數歸屬、系統／步驟狀態 |
| `xfer690/wikitable.py` | Markdown 表格定位、欄名檢查、只填空格、插列、手改衝突檢查 |
| `xfer690/wiki.py` | 填格規劃、Wiki.js GraphQL 用戶端、整頁同步與預覽 |
| `xfer690/s690.py` | s690 查詢與書目／雜項／影像抽測 |
| `xfer690/gmail_client.py` | Gmail REST 唯讀用戶端 |
| `xfer690/service.py` | 設定、抓信與快取、背景 s690 查詢、Gmail 授權、KM 登入與寫入 |
| `xfer690/authorize.py` | 一次性 Gmail 授權與信箱統計 |

## 程式碼規範與注意事項

- 依周邊程式碼的命名、縮排與註解密度修改；保留檔案原本的 LF／CRLF。腳本改檔後看 `git diff --stat`，避免行尾轉換造成整檔變動。
- 執行時設 `PYTHONDONTWRITEBYTECODE=1`，避免 Google Drive 同步各機的 `.pyc` 衝突副本。
- 網頁執行期版號唯一來源是 `core/traffic_monitor.py` 的 `VERSION`，由 `render_index()` 替換 `<!--#version-->`；下層模組不回頭匯入入口取版號。
- `stop_monitor.bat` 用「命令列含 `traffic_monitor.py`」找程序來停，這個檔名不能改；它也可能關掉另一份 flow check 的實例，測試時不要用它。
- 路徑集中在 `core/paths.py`。狀態、偏好與每日統計按機器分檔；逐分鐘明細放 `%LOCALAPPDATA%\flow_check\traffic\`，不要搬進同步夾。
- JSON 寫入優先沿用 `common.jsonio.write_json_atomic()`；共享可變狀態與同檔寫入沿用既有鎖，不繞過它們。
- 內網排除一律啟用；舊 `exclude_lan` 設定已不使用。`count_ranges` 優先於 `lan_ranges`，loopback 不列入計費／歸因。`10.0.0.0/8` 是刻意涵蓋全部 10.x，不能縮回 10.75/16。
- NIC 計費與 ESTATS 探測是兩個不同頻率的迴圈，不要混為一談；TCP 歸因對不到的 UDP／QUIC 與標頭等仍須留在未歸因用量。
- `report_apps` 可調整分類的程序檔名，但目前報表前端只單獨畫 `ssh`、`claude`、`vscode`；新增類別不會自動增加圖表系列。
- 690 不背景定時抓信：開網頁、按「立即更新」或 Gmail 授權完成才抓；每 60 秒讀快照不是抓 Gmail。
- KM 自動填值的「只填空格」、欄名不符不寫、目標列以外原文不變、手改須驗證原值等規則不得放寬。每日無資料值在補跑成功後可被自動成功值替換，其他非空值仍保護。
- KM JWT 以 `wiki_jwt` 存在 `data/config/xfer690_secrets.json`，登入／續期／失效時同步更新；不得回顯到網頁、snapshot、事件快取或 log。Gmail token 與 OAuth credentials 也不得進版控或同步到 TIPO。
- 改 Python 檔或 `config.json` 後要重啟；改 `web/` 後重整即可。`xfer690.json` 抓信時重讀，KM 登入也會重讀。

## 新增端點與頁籤

- HTTP 端點在 `core/routes.py` 新增函式，再加入 `build_routes()`；以 `(method, path)` 精確比對，註冊順序無關。非 200 回應沿用 `HttpError`，純文字等回應用 `Raw`。690 有副作用的 POST 保留 `_json_only()` 檢查。
- 新頁籤新增 `web/partials/xxx.html` 與 `web/js/xxx.js`，檔名限小寫英數與底線、首字母為英文字母；兩個檔案須同名配對。面板外層用 `<div class="panel hidden" id="panel-xxx">`，JS 呼叫 `registerTab({id, label, order, onInit?, onFirstShow?, onShow?})`。
- `render_index()` 送頁時掃配對檔並注入，不需改 `web/index.html`。JS 是傳統 script、共用全域作用域：`util.js` 最先，頁籤模組按檔名字母序，`main.js` 最後；不要假設 ES module 的隔離與匯入機制。

## 測試

- `tests/` 只放本機、不進版控，但改程式時要補自動測試。測試用暫存資料與假用戶端，不碰正式 `data/`、Gmail 或 KM。
- 在 flow_check 根目錄用 Bash 執行：
  - 主測試：`PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 py -3.13 -m unittest discover -s tests`
  - 同步工具測試：`PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 py -3.13 -m unittest discover -s tools -p "test_*.py"`
- 不在這份文件固定測試總數；以當次實際結果為準。
- 畫面驗證用 **8798 測試伺服器 + Playwright**，不停止或改動正在跑的 8765，不改正式 `config.json` 的 port。

### 8798 測試啟動方式

程式沒有 port 命令列參數。把以下內容存成**本次 session scratchpad** 的 `run_test.py`，在 flow_check 根目錄以 Bash 執行 `PYTHONUTF8=1 py -3.13 <scratchpad>/run_test.py .`（`<scratchpad>` 換成本次的絕對路徑；含空白時加引號）。

```python
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

source = Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
    root = Path(folder)
    for name in ("program", "web"):
        shutil.copytree(source / name, root / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    cfg = json.loads((source / "config.json").read_text(encoding="utf-8"))
    cfg["port"] = 8798
    (root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    data_config = root / "data" / "config"
    data_config.mkdir(parents=True)
    (data_config / "xfer690.json").write_text(
        '{"enabled": false, "s690_enabled": false}', encoding="utf-8")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
               LOCALAPPDATA=str(root / "local"))
    launcher = ("import runpy, sys, webbrowser; "
                "webbrowser.open = lambda *a, **k: False; "
                "runpy.run_path(sys.argv[1], run_name='__main__')")
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", launcher,
         str(root / "program" / "core" / "traffic_monitor.py")],
        cwd=root, env=env)
    try:
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait()
```

- 背景啟動後等 `http://127.0.0.1:8798/api/status` 回 200，再由 Playwright 開首頁；結束只停止該背景工作，不執行 `stop_monitor.bat`。
- 上述方式只複製程式／網頁與非機密主設定，資料與用量明細全在 scratchpad，預設停用 690；不複製正式 token、JWT 或歷史資料。
- 驗證 690 畫面時，用人工／測試 fixture 與假 Gmail、KM、s690 用戶端替換服務；不要為了看畫面使用正式憑證或觸發外部寫入。

## 同步 TIPO flow check

- TIPO flow check（`G:\我的雲端硬碟\TIPO flow check`）由 `tools/sync_tipo.py` 產生，不可手動改。
- `tools/sync_tipo.json` 定義 `copy`／`exclude`／`readme`／`protect`／`rewrite`。新增 core／common 或網頁檔時確認 TIPO 是否需要；頁籤 HTML 與 JS 的同步清單也要配對。
- `rewrite` 對 `core/paths.py` 的資料路徑與 `start_monitor.bat` 的排程名稱用完整字串替換；修改被匹配的程式行時，要一起核對更新 rewrite，否則同步驗證會失敗或 TIPO 路徑不對。
- 690 的相依放 `requirements-690.txt`，不要加到會同步的 `requirements.txt`；`program/xfer690/`、690 網頁及機密檔不在同步清單。
- 同步時在 flow_check 根目錄照抄以下 Bash 指令（符合 `.claude/settings.local.json` 允許清單）：
  - 預覽：`PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 py -3.13 tools/sync_tipo.py`
  - 寫入：`echo y | PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 py -3.13 tools/sync_tipo.py --apply`
- 寫入前後各算一次 TIPO 的 `data/`、`config.json`、每個 `desktop.ini` 的 SHA-256，確認完全相同；寫入後再跑一次預覽，應該是 0 個變更。不用另外壓縮備份。
- 同步完要同時 commit。

## 文件分工

- 根目錄 `README.html` 是使用者首頁：介紹、安裝、啟動、功能與第一次設定。
- `doc/進階參考.html` 放 690 規則、設定速查、使用者會用到的檔案與 HTTP API；設定說明只寫在這裡，不在其他文件重複。新增設定鍵或端點時要一起更新。
- 這兩份目前不是自動產生，相關內容變動時要一起更新。
- 本檔放 AI 開發需要的架構、模組職責、規範、測試與注意事項；README 不再複製完整模組表或開發流程。
- `doc/TIPO flow check README.md` 是 TIPO 使用者說明的來源，由同步工具複製成目標的 README；只在來源修改。
- 進度表 `doc/plans/進度.md` 只放未完成的事，做完就刪。更新時可直接整節改寫，不用另外問；長期規則放本檔，不放待辦。
- 有使用者看得到的功能／介面改動，要一起更新 `doc/版本紀錄.html`；TIPO 適用的流量條目也照抄到來源 README，透過同步工具帶過去。

## 介面風格

- 說明文字越少越好：不加提醒框或說明卡片，說明收進標題旁的資訊圖示；手動改過的格子只用背景色標示；無資料時只留「查看 log」。
- 元件樣式要統一，改完之後深色、淺色、原版三個主題都要檢查一遍。
- 深色／淺色共用 `web/style.css` 的 token（`<html data-theme>`）；原版用 `web/style-classic.css`。
- 新 CSS class 不要和全域樣式撞名（例如 `.sub`）；690 專用樣式沿用 `x6-` 前綴。淺色主題的 `--pop-bg` 是深紫，淺底配色的內容不要放在它上面。
