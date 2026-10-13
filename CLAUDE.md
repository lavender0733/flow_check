# CLAUDE.md

## 命名

- 叫「**TIPO flow check**」，不要叫「同事版」；叫「**KM**」，不要叫「Wiki」。

## 工作流程

- 只叫你「閱讀」計畫或文件時，先不要動手，看完先確認待決事項。
- feat 分支等全部完成後才合併回 main。
- commit 只在我說了才做，訊息格式：`type(scope): 中文說明`，scope 用 `core`、`xfer690`、`docs` 等。
- 寫入 KM 有新的寫法時，先用最容易復原的方式試（例如只新增一行），確認沒問題再擴大範圍。

## 測試

- `tests/` 只放本機、不進版控，但改程式時要補自動測試並執行：
  `PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 py -3.13 -m unittest discover -s tests`
- 要看畫面時，用 8798 測試伺服器 + Playwright，不要動正在跑的 8765。

## 同步 TIPO flow check

- TIPO flow check（`G:\我的雲端硬碟\TIPO flow check`）由 `tools/sync_tipo.py` 產生，不可手動改。
- 同步時在 flow_check 根目錄照抄以下指令（只有這個寫法在 `.claude/settings.local.json` 的允許清單裡，換寫法會被擋）：
  - 預覽：`PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 py -3.13 tools/sync_tipo.py`
  - 寫入：`echo y | PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 py -3.13 tools/sync_tipo.py --apply`
- 寫入前後各算一次 TIPO 的 `data/`、`config.json`、每個 `desktop.ini` 的 SHA-256，確認完全相同；寫入後再跑一次預覽，應該是 0 個變更。不用另外壓縮備份。
- 同步完要同時 commit。

## 文件

- 進度表 `doc/plans/進度.md` 只放未完成的事，做完就刪。更新時可直接整節改寫，不用另外問。
- 有使用者看得到的改動，要一起更新 `doc/版本紀錄.html`，TIPO 那份也照抄過去。

## 介面風格

- 說明文字越少越好：不加提醒框或說明卡片，說明收進標題旁的資訊圖示；手動改過的格子只用背景色標示；無資料時只留「查看 log」。
- 元件樣式要統一，改完之後所有主題都要檢查一遍。
