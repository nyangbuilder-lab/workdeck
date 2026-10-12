@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Workdeck 설치

rem 1) 파이썬 찾기: py 런처 → python 순서
set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY python --version >nul 2>nul && set "PY=python"
if defined PY goto run

rem 2) 파이썬이 없으면 윈도우 기본 설치 도구(winget)로 자동 설치
echo.
echo  Python이 없어서 지금 같이 설치할게요. 1~2분 걸려요.
echo  중간에 확인 창이 뜨면 [예]를 눌러주세요.
echo.
winget install -e --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
rem 방금 설치한 파이썬은 이 창의 PATH에 아직 안 잡혀서 경로로 직접 실행 (값에 따옴표 포함)
set PY="%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if exist %PY% goto run

echo.
echo  자동 설치가 안 됐어요. 아래 주소에서 Python을 직접 설치한 뒤 이 파일을 다시 실행해 주세요.
echo  https://www.python.org/downloads/
echo  설치 첫 화면에서 "Add python.exe to PATH" 를 꼭 체크해 주세요.
echo.
pause
exit /b 1

:run
%PY% workdeck.py install %*
echo.
pause
