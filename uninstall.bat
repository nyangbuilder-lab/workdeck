@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Workdeck 제거
taskkill /f /fi "WINDOWTITLE eq Workdeck" >nul 2>nul

set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY set PY="%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
%PY% workdeck.py uninstall
echo.
pause
