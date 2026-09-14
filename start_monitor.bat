@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "TASKNAME=FlowCheckTrafficMonitor"

REM ── Run-once UAC: elevate through a Scheduled Task so we only prompt the FIRST time ──
REM The LAN-traffic exclusion reads per-connection byte counts via the Windows ESTATS
REM API (GetPerTcpConnectionEStats). Enabling that needs admin - without it the call
REM returns ACCESS_DENIED and internal transfers (FileZilla etc.) cannot be subtracted.
REM UAC normally re-prompts on EVERY elevation, which is painful when restarting the
REM tool repeatedly during development. So the first run registers a Highest-privilege
REM scheduled task (one UAC prompt); every later launch just triggers that task, which
REM runs elevated in your session with NO prompt. (fltmc succeeds only if elevated;
REM do NOT use "net session" - it also needs the Server service, which is disabled
REM on some machines, so it fails even when elevated and the script loops forever.)

REM This branch is hit only when we relaunch ourselves elevated to register the task.
if /i "%~1"=="createtask" goto :createtask

REM Already elevated (i.e. the scheduled task launched us) -> run straight away.
fltmc >nul 2>&1
if %errorlevel% EQU 0 goto :elevated

REM Not elevated, but the task already exists -> trigger it (elevated, no UAC) and exit.
schtasks /query /tn "%TASKNAME%" >nul 2>&1
if %errorlevel% EQU 0 (
  schtasks /run /tn "%TASKNAME%" >nul
  exit /b
)

REM First run on this machine: relaunch elevated once to register the task (UAC here).
echo First-time setup: creating an elevated task so future launches won't prompt for admin...
powershell -NoProfile -Command "try { Start-Process -FilePath '%~f0' -ArgumentList 'createtask' -Verb RunAs } catch { exit 1 }"
if %errorlevel% EQU 0 exit /b
echo [warn] Admin declined - starting WITHOUT LAN exclusion (total traffic still counted).
goto :elevated

:createtask
REM (elevated) Register the task to run THIS bat at Highest level in the interactive
REM session, with no run-time limit, then start it now. Paths with spaces/Unicode are
REM passed via env vars to sidestep schtasks/PowerShell quoting issues.
set "SELF=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$a=New-ScheduledTaskAction -Execute $env:SELF; $p=New-ScheduledTaskPrincipal -UserId \"$env:USERDOMAIN\$env:USERNAME\" -LogonType Interactive -RunLevel Highest; $s=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero); Register-ScheduledTask -TaskName $env:TASKNAME -Action $a -Principal $p -Settings $s -Force | Out-Null"
schtasks /run /tn "%TASKNAME%" >nul
exit /b

:elevated
cd /d "%~dp0"

REM Keep .pyc out of this Google Drive folder: each machine's __pycache__ would
REM sync against the other and create conflicted copies, wasting sync bandwidth.
set PYTHONDONTWRITEBYTECODE=1

REM Check Python is available (this tool launches with "python", must be on PATH).
python --version >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python and tick "Add Python to PATH".
  echo         Download: https://www.python.org/downloads/
  pause
  exit /b
)

REM Ensure psutil is installed; auto-install on first run on a new machine.
python -c "import psutil" >nul 2>&1
if errorlevel 1 (
  echo Required package psutil is missing - installing...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] psutil install failed. Run manually: python -m pip install psutil
    pause
    exit /b
  )
)

REM Ensure openpyxl is installed (used to merge query results into one Excel per machine).
python -c "import openpyxl" >nul 2>&1
if errorlevel 1 (
  echo Package openpyxl is missing - installing...
  python -m pip install openpyxl
  if errorlevel 1 echo [WARN] openpyxl install failed; queries still work, but output won't be merged into Excel.
)

REM === Traditional (front-end) retrieval needs playwright + chromium ===
REM Packages must go to the interpreter named in config.json gpss_python (default py -3.13);
REM probe whether py -3.13 is usable, fall back to python (matches traffic_monitor).
REM Note: uses goto flow rather than nested parentheses to avoid cmd parsing issues.
set "GPSSPY=py -3.13"
%GPSSPY% --version >nul 2>&1
if errorlevel 1 set "GPSSPY=python"

%GPSSPY% -c "import playwright" >nul 2>&1
if not errorlevel 1 goto pw_ok
echo playwright (for traditional retrieval) is not installed - installing now (incl. chromium; first time takes a few minutes)...
%GPSSPY% -m pip install playwright
if errorlevel 1 goto pw_fail
%GPSSPY% -m playwright install chromium
if errorlevel 1 goto pw_fail
goto pw_ok
:pw_fail
echo [WARN] playwright/chromium install failed; API retrieval still works, traditional retrieval will not.
echo         Install later manually: %GPSSPY% -m pip install playwright
echo         Then: %GPSSPY% -m playwright install chromium
:pw_ok

echo Starting traffic monitor... the browser opens once the server is ready. Press Ctrl+C here to stop.
REM The browser is opened by traffic_monitor.py after the port binds (see its main()).
REM We do NOT "start" the browser here, otherwise it opens before the server is ready and
REM shows "cannot reach 127.0.0.1", needing a refresh (worst right after reconnecting to
REM the network, while Google Drive is busy syncing).
python program\core\traffic_monitor.py
pause
