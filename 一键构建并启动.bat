@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在构建线索池（第一次可能要 20-40 分钟）...
python build_leads.py
echo.
echo 启动看板...
python app.py
pause
