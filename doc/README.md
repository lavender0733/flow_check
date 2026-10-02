# Flow Check — 流量監控 + 690 資轉監控  `v8.0.0`

一個本機小工具:**量測當日網路用量**(對每日 2GB 額度),並提供**流量報表**,拿來跟公司反映連線問題。背景常駐、開瀏覽器即看。**690 資轉監控**預定在 8.1.0 加入。

## 使用的技術

- Python 3.13
- psutil 7.2.2(讀網卡累積位元組)
- Windows ESTATS(逐條 TCP 連線的收/發位元組;內網排除與程式歸因用)
- HTML / CSS / JavaScript(免打包、無框架)

---

## 安裝說明

1. 電腦要有 **Python**(安裝時勾「Add Python to PATH」)。
2. 把整個 `flow_check` 資料夾放到那台電腦(或放在 Google Drive 同步夾裡)。
3. 第一次啟動時會自動 `pip install psutil`,不用另外裝。

多台電腦共用同一個 Google Drive 資料夾也可以:各機以**主機名稱**自動分檔、不互相覆蓋,流量各自獨立計算(每台只看自己的當日用量)。

---

## 啟動 / 關閉

| 動作 | 做法 |
|---|---|
| 啟動 | 雙擊 **`start_monitor.bat`**(第一次會跳一次 **UAC**,按「是」;之後自動開瀏覽器到 `http://127.0.0.1:8765`) |
| 關閉 | 雙擊 **`stop_monitor.bat`**(同樣跳一次 UAC;只關本工具,不影響其他 Python) |
| 內網排除診斷 | 雙擊 **`diag_lan.bat`**(需管理員)。每 10 秒逐連線列出對方 IP、內外網分類與收發量,並和執行中主工具的實扣量對照 |

> **為什麼要管理員身分**:內網排除靠 Windows ESTATS 取「每條 TCP 連線的收/發 byte」,
> 啟用該收集是特權操作 —— 非管理員拿不到數字。第一次啟動會跳一次 UAC 並建立排程工作
> (`FlowCheckTrafficMonitor`),之後啟動直接觸發那個排程,不再詢問。**按取消也能跑**,
> 只是內網排除/暴增歸因停用、總流量照常計(視窗頂端會提示)。關機即結束;開機後需再雙擊啟動。
>
> 改完 `program/core/` 或 `program/common/` 下的任何 `.py` 或 `config.json` 後需重新啟動才生效
> (網頁上方會自動跳「需重啟」橫幅提醒);改 `web/` 下的 `.html`/`.js`/`.css` 重整即可。

---

## 流量監控

- 讀所有網卡的**累積位元組**(排除回環/隧道介面),精準累計當日上+下行對 2GB。
- 顯示已用 / 剩餘 / 即時速率 / 已排除(內網);接近上限圓環變色。
- **排除內網流量**開關(預設開):每隔 `lan_probe_interval_sec` 秒用 ESTATS 取**每條 TCP 連線**的收/發 byte,
  按**對方 IP** 判內/外網 —— 落在 `lan_ranges`(且不在 `count_ranges`)的連線其增量從當日額度扣除。
  IPv4 與 IPv6 都收;同一支程式的內、外網連線會各自分開計。需管理員身分。
- **用量通知**開關:已用比例達 N%(預設 80%)就跳頁內提醒視窗 + 紅色橫幅 + 分頁圖示紅點。同一天同門檻只提醒一次。
- **流量暴增預警**開關:某次取樣的用量同時①超過門檻(預設 50MB)②達近期中位數數倍時,跳橫幅並列出當下耗流量最多的程式。
- **限速偵測(網速)**:**被動**看近 N 分最高下載速率(不耗流量);**主動**按「測速」抓一小段量實際 Mbps、對「正常速率下限」判定(每次約耗 8MB)。
- **明細面板**(可收合、可切頁籤):
  - **網卡**:各網卡即時收發加總。
  - **今日程式排行**:今天每支程式累計吃掉多少額度(已扣內網),分「應用程式」(軟體名)與「程序名稱」(exe)兩欄,
    另有「**未歸因**」(對不到連線的 UDP/QUIC 與封包標頭)與「**監控未執行期間**」。額度莫名用完時先看這裡。
  - **用量明細**:每分鐘一筆計費明細。**由伺服器記錄,網頁沒開也照記**,保留 31 天,可切換日期、勾「≥1 MB」只看有量的分鐘。
    明細檔放本機 `%LOCALAPPDATA%\flow_check\traffic\`,刻意不放 Google Drive 同步夾(每分鐘都在長,放同步夾會讓 Drive 一直重傳)。
  - **暴增紀錄**:依應用程式與時間收合,展開看各 IP、PID 與流量。
- 開關與門檻、外觀都存在**本機偏好檔** `data/machines/prefs_<機器>.json`,跨重開、換瀏覽器都留得住,**以伺服器為準**。
- 程式碼更新但仍跑舊版、或資料停更/累積錯誤時,頁面上方跳橫幅提醒重啟。

## 流量報表

- **每日流量**:每天一根堆疊柱,由下往上 **ssh**、**Claude Code**、**VS Code**、其他。ssh 貼著底線放,最好比。
  可切「合計 / 上傳 / 下載」、期間「最近 14 天 / 31 天 / 全部」。
- **逐時**:點某一天,換成那天 8–20 點的逐時折線圖,三支程式各一條線、同一個刻度,看得出是不是同時段一起升降。按「← 每日」回來。
- **看每日數字**:展開表格看每天各程式與總量。
- 資料來自**每日用量統計** `data/machines/daily_stats_<機器>.json`:每天一筆、長期保存,換日時結算(今天那筆即時算)。
  每筆有總量、各程式量、未歸因、監控未執行期間、已排除內網量,以及逐時明細。
- 要分開統計的程式在 `config.json` 的 `report_apps` 調整(預設 `ssh` → `ssh.exe`、`vscode` → `code.exe`、`claude` → `claude.exe`)。
  只算 ESTATS 對得到的外網 TCP 連線;「未歸因」大代表各程式的量偏低。

## 外觀

右上角選單:**深色(預設)/ 淺色 / 原版**。深色與淺色共用 `web/style.css`(靠 `<html data-theme>` 換 token),
原版是 7.4.0 改版前的設計、完整保留在 `web/style-classic.css`。選擇同時存瀏覽器(開頁時搶在第一次繪製前套上,不閃色)
與伺服器本機偏好檔(以伺服器為準)。

---

## 檔案結構

> **要加新功能時:**
> - **新增一個 HTTP 端點** → 在 `program/core/routes.py` 寫一個函式,在 `build_routes()` 加一行註冊。路徑是**精確比對**,註冊順序無關緊要。
> - **新增一個網頁頁籤** → 丟兩個新檔:`web/partials/xxx.html`(面板,外層 `<div class="panel hidden" id="panel-xxx">`)與
>   `web/js/xxx.js`(結尾呼叫 `registerTab({id, label, order, onInit?, onFirstShow?, onShow?})`)。**現有檔案零改動**,重整即生效。
> - **同事版(TIPO flow check)**由 `同步到同事版.bat` 從這裡產生。在 `program/core/` 新增檔案時,要想清楚同事版需不需要
>   (`tools/sync_tipo.json` 的 `copy` / `exclude`)。

```
flow_check/
├─ README.html            本說明文件(雙擊即可在瀏覽器看)
├─ doc/
│  ├─ README.md            本說明文件的 Markdown 版
│  ├─ 進階參考.html         設定速查、檔案結構、HTTP API 端點
│  ├─ 版本紀錄.html         各版更新紀錄
│  ├─ 同事版README.md       同事版 README 的來源(同步時複製成 TIPO flow check 的 README.md)
│  ├─ specs/               設計文件
│  └─ plans/               實作計畫與進度筆記
├─ start_monitor.bat / stop_monitor.bat   啟動 / 關閉(啟動時自動檢查並安裝 psutil)
├─ diag_lan.bat           內網排除診斷(需管理員)
├─ 同步到同事版.bat        產生同事版(雙擊只預覽;命令列加 --apply 才寫入)
├─ requirements.txt       第三方相依套件(psutil)
├─ config.json            主設定:額度、port、取樣間隔、網段
├─ tools/                 開發用工具:sync_tipo.py(同步腳本)、sync_tipo.json(清單與路徑對應)、test_sync_tipo.py
├─ web/                   網頁介面(免打包純 HTML/JS;頁籤:流量監控 / 流量報表)
│  ├─ index.html          外殼:頁籤列由註冊表生成,面板與 js 由伺服器掃 partials/ 注入
│  ├─ style.css           樣式:深色 / 淺色共用一份骨架,靠 <html data-theme> 切 token
│  ├─ style-classic.css   「原版」外觀
│  ├─ partials/           各分頁 HTML(monitor / report)
│  └─ js/                 util / monitor / report / main(載入順序即相依順序、共用全域作用域)
├─ data/                  (不在版控內)
│  ├─ config/machines.json 機器代號 → 友善名稱對照(如「桌機」;空值則顯示代號)
│  └─ machines/            各電腦執行期資料(檔名含機器代號,同步不互相覆蓋)
│     ├─ state_<機器>.json        當日用量(跨重啟保留、跨日歸零)
│     ├─ prefs_<機器>.json        本機偏好:通知 / 暴增預警 / 排除內網開關與門檻、外觀
│     └─ daily_stats_<機器>.json  每日用量統計(流量報表)
└─ program/
   ├─ core/
   │  ├─ traffic_monitor.py   進入點:組裝與啟動(載設定 → 造 Monitor → 起背景迴圈 → 開伺服器;VERSION 版號唯一來源)
   │  ├─ routes.py            HTTP 端點,各一個函式,最下方 build_routes() 一張註冊表
   │  ├─ sampler.py           流量取樣與計費(Monitor)、ESTATS 高頻探測迴圈、主動測速
   │  ├─ daily_stats.py       每日用量統計(流量報表)
   │  ├─ estats.py            Windows ESTATS 逐 TCP 連線收/發位元組(內網排除底層)
   │  ├─ procinfo.py          程序 → 軟體名稱 / Windows 服務名稱
   │  ├─ health.py            錯誤收集與「程式碼已更新、需重啟」偵測(掃 core/ + common/)
   │  ├─ lan_diag.py          內網排除診斷工具(由 diag_lan.bat 啟動)
   │  ├─ paths.py             路徑常數與機器身分
   │  └─ storage.py           資料層:設定 / 偏好 / 機器標籤
   └─ common/
      ├─ webapp.py            HTTP 底座:路由分派、靜態檔、路徑穿越防護、錯誤收斂、serve()
      ├─ jsonio.py            JSON 原子寫入 / 容錯讀取 / 檔案簽章
      └─ ident.py             機器代號 machine_id()
```

---

## 設定速查

**`config.json`**
| 鍵 | 意義 |
|---|---|
| `quota_gb` | 每日流量額度(預設 2) |
| `port` | 網頁埠(預設 8765) |
| `sample_interval_sec` | NIC 計費取樣間隔秒數(預設 60) |
| `lan_probe_interval_sec` | 內網 ESTATS 高頻探測間隔秒數(預設 1;越短越接得住短命內網連線) |
| `exclude_nics` | 計流量時要略過的網卡關鍵字(預設 `["Loopback","isatap","Teredo"]`) |
| `exclude_lan` | 是否排除內網流量(預設 `true`;前端開關以 `prefs_<機器>.json` 為準) |
| `lan_ranges` | ★ 視為內網、要排除的網段(預設 `10.0.0.0/8`/`172.16.0.0/12`/`192.168.0.0/16`/`169.254.0.0/16`。`10.0.0.0/8` 是刻意涵蓋所有 `10.x` 內網 —— 7.0.2 以前只排除 `10.75/16`,其他 `10.x` 內網漏排除、單日一度衝到 ~6GB) |
| `count_ranges` | 強制計入白名單,優先於 `lan_ranges`(預設空清單)。落在 `lan_ranges` 內、但實際走對外線路要算進額度的網段寫在這裡 |
| `local_ranges` | 本機(loopback)網段(預設 `127.0.0.0/8`、`::1/128`):不經網卡、不吃額度,整筆不列入計算與歸因 |
| `speed_floor_mbps` | 限速偵測:低於此(Mbps)判定「疑似被限速」(預設 5;頁面可臨時覆蓋) |
| `peak_window_min` | 被動「最高下載速率」的觀測視窗分鐘數(預設 10) |
| `speedtest_url` | 主動測速來源(預設 Cloudflare `https://speed.cloudflare.com/__down`;被擋可換) |
| `speedtest_bytes` / `speedtest_timeout` | 主動測速最多抓的位元組(預設 8MB)/ 最長秒數(預設 6) |
| `report_apps` | 流量報表要分開統計的程式(選填):`{"類別": ["程序檔名.exe", …]}` |

> **流量暴增預警 / 用量通知 / 外觀不放這裡**:`surge_enabled`/`surge_min_mb`/`surge_factor`/`surge_window_min`/`surge_cooldown_sec`、
> `notify_enabled`/`notify_threshold` 與 `theme` 以**本機偏好** `data/machines/prefs_<機器>.json` 為權威來源(各台獨立、頁面即時切換);
> `config.json` 未設時走程式內建預設(暴增門檻 50MB、4×、視窗 30 分、冷卻 300 秒;通知門檻 80%;外觀深色)。
> 另有 `debug_lan`(預設 `false`):開啟後每次取樣把內網扣除明細印到主控台。

---

## HTTP API 端點(供開發 / 排查;一般使用者不需碰)

> 端點定義在 `program/core/routes.py` 的 `build_routes()`,一個端點一個函式。
> 路徑為**精確比對**;對不到的路徑一律回 404。

| 方法 | 路徑 | 用途 |
|---|---|---|
| GET | `/api/status` | 當日流量 / 即時速率 / 各網卡明細 / 健康狀態 / 暴增事件 / 今日程式排行 / `lan_excluded_bytes` 等(前端 5 秒輪詢) |
| GET | `/api/traffic/log?date=YYYY-MM-DD` | 某天的逐分鐘用量明細,外加有明細的日期清單 |
| GET | `/api/traffic/daily` | 每日用量統計(流量報表),今天那筆即時計算 |
| GET | `/api/config` | 額度、速率下限、本機偏好(`notify_*`、`surge_*`、`exclude_lan`、`theme`) |
| POST | `/api/prefs` | 寫回本機偏好,即時套用到執行中的監控(不必重啟) |
| POST | `/api/speedtest` | 主動測速:抓一小段量實際 Mbps、判斷有無被限速(每次約耗 8MB) |

---

## 版本紀錄

見 [版本紀錄.html](版本紀錄.html)。

## 舊版

1.0.0 – 7.6.0 是 GPSS 批次檢索時代,已封存於 `G:\我的雲端硬碟\flow_check_v7.6.0_GPSS最終版_2026-10-02.zip`,git 標籤 `v7.6.0-gpss-final`。
