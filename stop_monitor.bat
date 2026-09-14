@echo off
chcp 65001 >nul

REM Self-elevate: the monitor runs as administrator (for ESTATS LAN exclusion),
REM and a non-elevated process cannot terminate an elevated one. So we elevate too.
REM (fltmc needs admin but no service; "net session" breaks - see start_monitor.bat.)
fltmc >nul 2>&1
if %errorlevel% EQU 0 goto :elevated
if /i "%~1"=="elevated" goto :elevated
powershell -NoProfile -Command "try { Start-Process -FilePath '%~f0' -ArgumentList 'elevated' -Verb RunAs } catch { exit 1 }"
if %errorlevel% EQU 0 exit /b
echo [warn] Admin declined - cannot stop a monitor that is running as administrator.
:elevated
echo Stopping traffic monitor (only this tool; other Python processes are untouched)...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'pythonw.exe' -or $_.Name -eq 'python.exe') -and $_.CommandLine -like '*traffic_monitor.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Done.
pause
