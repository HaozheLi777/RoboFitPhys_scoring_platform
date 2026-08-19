@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo 未找到 Python。请先安装 Python 3.10 或更高版本,
  echo 然后执行: pip install -r requirements.txt
  pause
  exit /b 1
)

python run.py
pause
