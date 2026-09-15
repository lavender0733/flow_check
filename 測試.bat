@echo off
cd /d "%~dp0"
REM 別在這個 Google Drive 同步夾裡寫 .pyc（避免 __pycache__ 跨機同步衝突）。
set PYTHONDONTWRITEBYTECODE=1
rem 先偵測 py -3.13（與 config.json 的 gpss_python 一致），不可用則退回 python
py -3.13 --version >nul 2>nul
if errorlevel 1 (
  python -m unittest discover -s tests -v
) else (
  py -3.13 -m unittest discover -s tests -v
)
echo.
pause
