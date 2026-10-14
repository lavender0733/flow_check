# Flow Check — 流量監控 + 690 資轉監控  `v8.1.0`

> 本檔是完整使用者說明的正本。[精簡首頁](../README.html)方便瀏覽，[進階參考](進階參考.html)提供設定速查與 HTTP API；開發架構與規範見 [.claude/CLAUDE.md](../.claude/CLAUDE.md)。

一個本機小工具:**量測當日網路用量**(對每日 2GB 額度),並提供**流量報表**,拿來跟公司反映連線問題。背景常駐、開瀏覽器即看。
**690 資轉監控**讀 s690 Rundeck 的通知信,顯示每個轉置系統每天 / 每期的狀態,並在 KM 的「資料轉置登記」表格裡登記。

## 使用的技術

- Python 3.13
- psutil 7.2.2(讀網卡累積位元組)
- Windows ESTATS(逐條 TCP 連線的收/發位元組;內網排除與程式歸因用)
- google-auth-oauthlib(690:Gmail API 唯讀授權)、Wiki.js 2.x GraphQL(690:寫入登記表)
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
| --- | --- |
| 啟動 | 雙擊 **`start_monitor.bat`**(第一次會跳一次 **UAC**,按「是」;之後自動開瀏覽器到 `http://127.0.0.1:8765`) |
| 關閉 | 雙擊 **`stop_monitor.bat`**(同樣跳一次 UAC;只關本工具,不影響其他 Python) |
| 內網排除診斷 | 雙擊 **`diag_lan.bat`**(需管理員)。每 10 秒逐連線列出對方 IP、內外網分類與收發量,並和執行中主工具的實扣量對照 |

> **為什麼要管理員身分**:內網排除靠 Windows ESTATS 取「每條 TCP 連線的收/發 byte」,
> 啟用該收集是特權操作 —— 非管理員拿不到數字。第一次啟動會跳一次 UAC 並建立排程工作
> (`FlowCheckTrafficMonitor`),之後啟動直接觸發那個排程,不再詢問。**按取消也能跑**,
> 只是內網排除/暴增歸因停用、總流量照常計(視窗頂端會提示)。關機即結束;開機後需再雙擊啟動。
>
> 改完 `program/core/`、`program/common/` 或 `program/xfer690/` 下的任何 `.py` 或 `config.json` 後需重新啟動才生效
> (網頁上方會自動跳「需重啟」橫幅提醒);改 `web/` 下的 `.html`/`.js`/`.css` 重整即可。

---

## 流量監控

- 讀所有網卡的**累積位元組**(排除回環/隧道介面),精準累計當日上+下行對 2GB。
- 顯示已用 / 剩餘 / 即時速率 / 已排除(內網);接近上限圓環變色。
- **排除內網流量**(一律開啟,沒有開關):每隔 `lan_probe_interval_sec` 秒用 ESTATS 取**每條 TCP 連線**的收/發 byte,
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
  可更換這三類的程序檔名;目前圖表只單獨顯示 `ssh`、`claude`、`vscode`,新增類別不會自動增加圖表系列。
  只算 ESTATS 對得到的外網 TCP 連線;「未歸因」大代表各程式的量偏低。

## 外觀

右上角選單:**深色(預設)/ 淺色 / 原版**。深色與淺色共用 `web/style.css`(靠 `<html data-theme>` 換 token),
原版是 7.4.0 改版前的設計、完整保留在 `web/style-classic.css`。選擇同時存瀏覽器(開頁時搶在第一次繪製前套上,不閃色)
與伺服器本機偏好檔(以伺服器為準)。

---

## 690 資轉監控

s690 的 Rundeck 每跑一個步驟就寄一封通知信(主旨像 `🔴【FAILURE】[s690] 012001-techreport-pdf-step1-raw | 01-tw/20-techreport-pdf`)。
網頁打開時自動從 Gmail 讀一次這些信,之後按「立即更新」才再讀(不在背景定時抓),判定每個轉置系統每天(或每一期)、每個步驟是成功、失敗、無資料還是沒來。
Rundeck 在內網連不到,所以只靠信。

### 第一次設定(每台電腦一次)

1. `data/config/gmail_credentials.json`:GCP 的 OAuth 用戶端(電腦版應用程式,範圍只要 `gmail.readonly`)。已經放好就跳過。
2. 在「信件與連線」的 Gmail 卡片按 **授權**(或雙擊 **`690授權.bat`**)→ 新分頁跳出 Google 登入,用 `alicia.huang@innovue.ltd` 同意唯讀權限。
   Gmail 走公開的 Google 端點,不用開 VPN、不用換網路。分頁被關掉就按「重新開啟授權頁」。
   已授權時卡片顯示 **登出**(刪掉 token、停止抓信)。
   使用 `690授權.bat` 完成後會印出近 30 天每個 job 的統計表(封數、成功失敗、時刻),拿來核對系統清單。
   token 存在 `data/machines/gmail_token_<機器>.json`。
3. 要寫 KM 的話,`xfer690.json` 的 `wiki.enabled` 要是 `true`。KM 只開 **Innovue OAuth** 登入,程式沒辦法自己登入:
   在瀏覽器用 Innovue OAuth 登入 KM → F12 → Application → Cookies → 複製 `jwt` 的值 →
   貼到「信件與連線」的 KM 卡片按「登入 KM」。伺服器先跟 KM 確認有效才收下,存進
   `data/config/xfer690_secrets.json` 的 `wiki_jwt`(重開不用重貼;KM 續期發的新憑證會自動寫回;失效時清掉、卡片要你重貼)。
   不開 VPN 也能用:那時 KM 走 Cloudflare,它擋 Python 預設的 User-Agent,程式送自己的
   `flow-check-xfer690/1`。被閘道擋下(HTTP 401/403)算網路問題,不會清掉 `wiki_jwt`。
4. 重開 `start_monitor.bat`。第一次會自動安裝 `requirements-690.txt` 的套件;裝不起來只會停用 690,流量監控照常。

> 三個機密檔(`gmail_credentials.json`、`gmail_token_<機器>.json`、`xfer690_secrets.json`)都不在版控內,
> 也不會同步到 TIPO flow check。KM 登入憑證只存在 `xfer690_secrets.json`,網頁送出後不會再回傳或顯示。

### 畫面

- **690資轉**頁籤,分類列:
  - **每日資轉**:今天每個每日系統一塊磚,依最晚時間排;失敗、該到沒到等需要處理的整塊淡紅底。
    磚裡有狀態、收信或最晚時間,以及步驟一顆點(步驟和它的 `-check` 併成一顆,兩個都成功才算綠)。
    系統有設定 `reruns` 的(目前是搜尋後續),磚塊底下多一行「補跑」:近 7 天每個補跑作業只看最新一次,
    成功的一顆綠點,失敗的寫出作業名稱和「失敗」log 連結,算在每日資轉的警示。
    設了 `rerun_date` 的(目前是技術報告)補跑前幾天的資料時,也列在「補跑」,並算回資料那一天;
    KM 上那天原本填的「-」(`nodata_value`)補跑成功後改成 v,其他人填的值照樣不覆寫。
  - **TWA 公開 / TWB 公告**:資料種類(全文、書目、雜項、英譯)× 公告期數。只追蹤最近一個月(TWA 2 期、TWB 3 期);
    近一個月內補跑的舊期列在「更早的期」。點一格看那期的細節。
  - **其他作業**:信裡出現、但不在系統清單裡的 job。手動作業依作業名稱(去掉 `-stepN-…`)一個作業一塊磚(新的在前,失敗的整塊淡紅底),
    同名的不論成功或失敗只列最新一次(失敗會算警示);維運 99xxxx 只列最後一次。
  - **KM 登記**:登入 KM 後顯示「填好之後」的表格預覽,按「寫入 KM」才寫。
- **信件與連線**頁籤:Gmail 授權狀態、KM 登入(在這裡按「登入 KM」)、最近 20 封信。
- 失敗項目的「失敗」兩個字就是連結,點了打開那封信附的 `.log`,自動捲到 `error:` 那一行(伺服器當下抓,不存檔)。
- 分類上的紅色數字 = 需要處理的件數;主頁籤的 ⚠ = 有轉置警示(690資轉)或連線問題(信件與連線)。

### 狀態的意思

| 狀態 | 意思 |
| --- | --- |
| 成功 | 步驟與它的 check 都成功 |
| 無資料 | step1-raw 失敗,原因是 `GCS 資料夾無檔案`。**每日系統**不算警示,KM 那幾格填 `-`(`wiki.nodata_value`),不打 v;**期別系統**代表那期檔案還沒到,算警示、不寫 KM |
| 失敗 | 有失敗信。之後的步驟顯示「沒有執行」 |
| 該到沒到 | 過了 `expect_by` 還沒收到信;或一次執行收到幾步之後就沒信了(期別系統以最後一封信後 2 小時為期限) |
| 還沒跑到 | 還沒到 `expect_by` |
| 無法判定 | 主旨的狀態字不是 SUCCESS / FAILURE |

同一期跑了好幾次(例如前一天無資料、隔天重跑成功)以最後一次為準。
**不定期**的系統(全文、英譯)沒有排程,不會報「該到沒到」。

### 期別怎麼對應到 KM 的列

- 每日系統:一天一列,列鍵是執行日期(`2026/10/01`)。
- 書目、雜項、全文:列鍵是**公告期數**,取自 log 的 `RUNDECK:DATA:VOL`(手動補跑舊期也會歸到正確的期,不看收信日期)。
  期數與日期互換:TWB 每月 1、11、21 日,`VOL = (年−1973)×100 + (月−1)×3 + 第幾旬`;TWA 每月 1、16 日,`VOL = (年−2002)×100 + (月−1)×2 + 上/下半月`。
- 英譯(TW英譯En 頁):列鍵是**期數區間**(`5325 ~ 5327`),取自 log 的 `FILE_I`(公告)/ `FILE`(公開)檔名
  `TW__I_20260901_5325_to_20260921_5327_complete`。同一個區間重跑好幾次是同一列,每個步驟以最後一次為準。
  無資料(那批檔案還沒到)跟期別系統一樣算警示、不寫 KM。
- TW影像GCS 頁:列跟書目的期數走(書目有收到信的期),說明書 / 代表圖由 s690 抽查(見下面)。
- 一次手動執行可能連跑好幾期,所以同一系統的信以「step1-raw 那封」或「跟上一封相隔超過 2 小時」切段,段裡其他信沿用該段的期數。

### 寫 KM 的規則

- **只填空格**:格子已經有值就不動(`2026xxxx` 這種佔位字當空格);值不同在預覽裡用波浪底線標出。
- 打 `v` 的格:那一步**和它的 check** 都成功才打。
- 筆數欄(書目頁的 Biblio、Gazette SUD):填 L1 那封信 log 的 `writes`,而且**整期每一步都成功**才填。
- 缺列就照列鍵順序插入;列鍵比表格最舊的一列還舊時不插入。
- 目標列以外的每一行逐位元組不變;欄名對不上時整張表不寫;內容沒變不送更新;一頁失敗不影響其他頁。
- **手動修改**:KM 登記頁點任一格可改(Enter 存、Esc 取消),寫入時一起送出。這是唯一會覆寫非空值的路徑,
  而且伺服器會先確認頁面上那格還是你看到的值,不是就整筆不寫、列為衝突。
- 登入在「信件與連線」做一次,憑證存進 `data/config/xfer690_secrets.json` 的 `wiki_jwt`,重開不用重貼;失效時需重新登入。
- `xfer690.json` 的 `wiki.dry_run` 是 `true` 時,按「寫入 KM」只算結果、不真的寫。
- **s690 查詢**(抓完信後在背景查,整期成功才查;結果存在快取,同一期不重查,重跑過才重查):
  - 690查詢筆數:進階檢索、案件類型 TWA / TWB、`PD:[那期日期 TO 那期日期]` 的總筆數。跟 Biblio 不同時照填,預覽標波浪線。
  - SUD抽測案例 / 備註:從雜項 log 取這期有的雜項類型(不含公告目錄、發明公開目錄),
    `MS:(類型…) AND MS:<卷> AND MS:<期>` 取第一筆「雜項真的刊在這一期」的案件(最多看 120 筆)。
    讓與比對書目的目前專利權人、消滅比對案件狀態,其他類型只確認查得到。正確 → 案例填 `TW` + 公開 / 公告號、備註填 `成功`;
    不符或找不到 → 兩格都不填,預覽的 tooltip 寫原因。
  - TW影像GCS:`OGV:<卷> AND OGN:<期>` 的第一筆,有說明書 / 代表圖就打 `v`;「抽查案號」填那一筆的 `TW` + 公開 / 公告號(說明書 / 代表圖沒有也照填)。
  - s690 連不上不影響抓信與其他欄;`data/config/xfer690.json` 的 `s690_enabled` 可以關掉。

### 流量預算

抓信一律先只抓主旨與時間(每封約 2 KB),只有三種信才下載全文:失敗信、期別系統與英譯每段的第一封(帶出期數)、
要填筆數的步驟。單封超過 `max_raw_kb`(預設 512 KB)也不下載。實測:第一次抓 45 天 495 封共 2.75 MB,之後每輪幾 KB。

### `data/config/xfer690.json`

| 鍵 | 意義 |
| --- | --- |
| `enabled` | 690 監控開關 |
| `retain_days` / `history_days` | 快取保留天數(45)/ 每日系統往回看幾天(14) |
| `gmail_query` | Gmail 搜尋條件 |
| `status_map` / `nodata_re` | 主旨狀態字對應 / 判定「無資料」的錯誤訊息 |
| `max_raw_kb` | 單封超過這個大小就不下載全文 |
| `systems[]` | 系統清單:`id`、`label`、`view`(`daily` / `twa` / `twb`)、`type`(`full` / `biblio` / `sud` / `eng`)、`auto`(母 job 代碼)、`steps`(步驟代碼,`-check` 是下一號)、`reruns`(選填,補跑 job 的代碼前綴,例如 `0191`,顯示在該列的「補跑」)、`rerun_date`(選填,每日系統 log 裡資料日期的 key,例如 `VOL_DATE`;當天第二次以後、或過了 `expect_by` 才跑的執行會下載 log 看補的是哪天)、`schedule`(`daily` / `{"monthdays": [...]}` / `manual`)、`period`(`date` / `vol_twa` / `vol_twb` / `vol` / `none`)、`expect_by`(最晚幾點該收到信) |
| `wiki.enabled` / `wiki.dry_run` | 開放登入 / 只算不寫 |
| `wiki.base` | 子頁所在的路徑 |
| `s690_enabled` | 抓完信後是否查 s690(預設 true;0 筆 / 查不到案件的暫定結果 24 小時內每次抓信再查) |
| `wiki.nodata_value` | 每日系統無資料那天,要打 v 的格改填的值(預設 `-`) |
| `wiki.sud_ok_value` | SUD 抽測成功時「備註」格填的字(預設「成功」) |
| `wiki.tables[]` | 每張表的每個系統一筆:`page`、`table`(`TWA` / `TWB` / 空)、`after`(表格前面的標題文字;只在這個標題那一節裡找,到下一個同級標題為止)、`header_has`、`label_row`、`labels`(整列欄名;頁面上不一樣就整張表不寫,預覽顯示紅框)、`key_col`、`key`(`vol` 公告期數 / `date` 日期 / `span` 英譯的期數區間)、`new_row`(`{yyyymmdd}`、`{mm}`、`{dd}`、`{vol}`、`{key}` = 列鍵)、`cols`(`{欄號: {label, value: v/tag/writes/s690_count/s690_spec/s690_rep/s690_image_case/s690_sud_case/s690_sud_note, step, check}}`) |

**KM 表格改結構時**:欄位順序變了 → 改 `cols` 的欄號與 `label`(欄名對不上時程式會整張表不寫,在預覽裡顯示原因);
表格前的標題改了 → 改 `after`;新增系統 → 在 `systems` 加一筆,要登記的話在 `tables` 加對應的 target。
一個不在清單上的 job 想固定追蹤 → 加進 `systems`,它就從「其他作業」移到每日資轉或 TWA / TWB;
是某個系統的補跑 → 加進那個系統的 `reruns`,就從「其他作業」移到那一列底下的「補跑」。

---

## 使用者會用到的檔案

```text
flow_check/
├─ README.html                       精簡說明首頁
├─ doc/
│  ├─ README.md                      完整使用者說明正本
│  ├─ 進階參考.html                   設定速查、HTTP API
│  ├─ 版本紀錄.html                   更新紀錄
│  └─ TIPO flow check README.md       TIPO 使用者說明來源
├─ start_monitor.bat / stop_monitor.bat   啟動 / 關閉
├─ diag_lan.bat                       內網排除診斷
├─ 690授權.bat                        Gmail 授權與信箱統計(也可在網頁授權)
├─ 同步到TIPO flow check.bat           產生 TIPO flow check(雙擊只預覽)
├─ requirements.txt / requirements-690.txt   啟動時自動檢查的相依套件
├─ config.json                       額度、埠、取樣間隔、網段
└─ data/
   ├─ config/machines.json           機器代號 → 友善名稱對照
   ├─ config/xfer690.json            690 系統清單與 KM 表格設定
   ├─ config/gmail_credentials.json  ★機密:GCP OAuth 用戶端
   ├─ config/xfer690_secrets.json     ★機密:KM JWT(wiki_jwt)
   └─ machines/
      ├─ state_<機器>.json           當日用量
      ├─ prefs_<機器>.json           本機偏好與外觀
      ├─ daily_stats_<機器>.json     每日用量統計
      ├─ gmail_token_<機器>.json     ★機密:Gmail 授權
      └─ xfer690_<機器>.json         已抓信件與查詢結果快取
```

`data/config/machines.json` 與 `data/config/xfer690.json` 進版控,其他資料與機密檔不進版控。
逐分鐘明細另放本機 `%LOCALAPPDATA%\flow_check\traffic\`,不放同步夾。
`program/`、`web/` 與 `tools/` 的開發分工見 [.claude/CLAUDE.md](../.claude/CLAUDE.md)。

---

## 設定速查

**`config.json`**

| 鍵 | 意義 |
| --- | --- |
| `quota_gb` | 每日流量額度(預設 2) |
| `port` | 網頁埠(預設 8765) |
| `sample_interval_sec` | NIC 計費取樣間隔秒數(預設 60) |
| `lan_probe_interval_sec` | 內網 ESTATS 高頻探測間隔秒數(預設 1;越短越接得住短命內網連線) |
| `exclude_nics` | 計流量時要略過的網卡關鍵字(預設 `["Loopback","isatap","Teredo"]`) |
| `lan_ranges` | ★ 視為內網、要排除的網段(預設 `10.0.0.0/8`/`172.16.0.0/12`/`192.168.0.0/16`/`169.254.0.0/16`。`10.0.0.0/8` 是刻意涵蓋所有 `10.x` 內網 —— 7.0.2 以前只排除 `10.75/16`,其他 `10.x` 內網漏排除、單日一度衝到 ~6GB) |
| `count_ranges` | 強制計入白名單,優先於 `lan_ranges`(預設空清單)。落在 `lan_ranges` 內、但實際走對外線路要算進額度的網段寫在這裡 |
| `local_ranges` | 本機(loopback)網段(預設 `127.0.0.0/8`、`::1/128`):不經網卡、不吃額度,整筆不列入計算與歸因 |
| `speed_floor_mbps` | 限速偵測:低於此(Mbps)判定「疑似被限速」(預設 5;頁面可臨時覆蓋) |
| `peak_window_min` | 被動「最高下載速率」的觀測視窗分鐘數(預設 10) |
| `speedtest_url` | 主動測速來源(預設 Cloudflare `https://speed.cloudflare.com/__down`;被擋可換) |
| `speedtest_bytes` / `speedtest_timeout` | 主動測速最多抓的位元組(預設 8MB)/ 最長秒數(預設 6) |
| `report_apps` | 分類 → 程序檔名清單(選填):`{"類別": ["程序檔名.exe", …]}`。目前圖表只單獨顯示 `ssh`、`claude`、`vscode`;可更換檔名,新增類別不會自動增加圖表系列 |

> **流量暴增預警 / 用量通知 / 外觀不放這裡**:`surge_enabled`/`surge_min_mb`/`surge_factor`/`surge_window_min`/`surge_cooldown_sec`、
> `notify_enabled`/`notify_threshold` 與 `theme` 以**本機偏好** `data/machines/prefs_<機器>.json` 為權威來源(各台獨立、頁面即時切換);
> `config.json` 未設時走程式內建預設(暴增門檻 50MB、4×、視窗 30 分、冷卻 300 秒;通知門檻 80%;外觀深色)。
> 另有 `debug_lan`(預設 `false`):開啟後每次取樣把內網扣除明細印到主控台。

---

HTTP API 端點與排查參考見 [進階參考.html](進階參考.html)。

## 版本紀錄

見 [版本紀錄.html](版本紀錄.html)。

## 舊版

1.0.0 – 7.6.0 是 GPSS 批次檢索時代,已封存於 `G:\我的雲端硬碟\flow_check_v7.6.0_GPSS最終版_2026-10-02.zip`,git 標籤 `v7.6.0-gpss-final`。
