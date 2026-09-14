@echo off
chcp 65001 >nul
cd /d "%~dp0"
python program\yearly_stats\yearly_server.py
pause
