@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 파이썬 런처(py)가 있으면 그걸 쓰고, 없으면 python
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
%PY% --version >nul 2>nul || (
  echo Python이 설치되어 있지 않아요. https://www.python.org/downloads/ 에서 설치한 뒤 다시 실행해 주세요.
  echo 설치할 때 "Add python.exe to PATH" 를 꼭 체크해 주세요.
  pause
  exit /b 1
)
%PY% workdeck.py install %*
pause
