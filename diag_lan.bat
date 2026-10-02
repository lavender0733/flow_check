@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM -- Standalone LAN-exclusion diagnostic (does not touch the monitor) --
REM Prints per-direction NIC delta vs ESTATS payload vs what the model excludes,
REM plus a per-connection breakdown, so you can see whether a big download leaks
REM on the recv side or the sent (ACK) side. ESTATS needs admin, so self-elevate.
REM (fltmc needs admin but no service; "net session" breaks - see start_monitor.bat.)
REM The relaunch marker doubles as %1, so strip it before handing args to Python.
set "DIAG_ARG=%~1"
if /i "%DIAG_ARG%"=="elevated" set "DIAG_ARG="
fltmc >nul 2>&1
if %errorlevel% EQU 0 goto :elevated
if /i "%~1"=="elevated" goto :elevated
echo Requesting administrator privileges (ESTATS needs admin)...
powershell -NoProfile -Command "try { Start-Process -FilePath '%~f0' -ArgumentList 'elevated' -Verb RunAs } catch { exit 1 }"
if %errorlevel% EQU 0 exit /b
echo [warn] Admin declined - ESTATS unavailable, per-connection bytes cannot be read.
:elevated
cd /d "%~dp0"

REM Keep .pyc out of this Google Drive folder (avoids __pycache__ sync conflicts).
set PYTHONDONTWRITEBYTECODE=1
REM Force UTF-8 so the Python script's Chinese report prints correctly.
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

python --version >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python and tick "Add Python to PATH".
  pause
  exit /b
)
python -c "import psutil" >nul 2>&1
if errorlevel 1 (
  echo Required package psutil is missing - installing...
  python -m pip install -r requirements.txt
)

echo Starting diagnostic (a report every 10s). Download the large file and watch
echo the "sent" row's "billed" amount. Press Ctrl+C to stop.
echo.
python program\core\lan_diag.py %DIAG_ARG%

echo.
echo --- diagnostic ended (window kept so you can scroll up) ---
pause
