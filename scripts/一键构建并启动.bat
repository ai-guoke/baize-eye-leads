@echo off
chcp 65001 >nul
cd /d "%~dp0\.."
echo 启动白泽之眼（Doris API）...
python -m uvicorn app.main:app --host 0.0.0.0 --port 8765
pause
