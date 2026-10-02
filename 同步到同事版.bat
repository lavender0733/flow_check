@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM Build the colleague copy (TIPO flow check) from this repo.
REM Double-click = preview only. To actually write: run "同步到同事版.bat --apply" from a command prompt.
set PYTHONDONTWRITEBYTECODE=1
py -3.13 tools\sync_tipo.py %*
pause
