@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
REM 別在這個 Google Drive 同步夾裡寫 .pyc(避免 __pycache__ 跨機同步衝突、拖慢真資料同步)。
set PYTHONDONTWRITEBYTECODE=1
cd /d "%~dp0"

rem 先偵測 py -3.13（與 config.json 的 gpss_python 一致），不可用則退回 python
py -3.13 --version >nul 2>nul
if errorlevel 1 (
  python program\core\monthly_progress.py
) else (
  py -3.13 program\core\monthly_progress.py
)

echo.
pause
