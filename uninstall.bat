@echo off
chcp 65001 >nul
cd /d "%~dp0"
taskkill /f /fi "WINDOWTITLE eq Workdeck" >nul 2>nul
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
%PY% workdeck.py uninstall
pause
