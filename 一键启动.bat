@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ==========================================
echo   雨课堂自动学习助手 - 一键环境检查与启动
echo ==========================================

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.10 及以上版本。
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/2] 首次运行，正在创建独立虚拟环境 .venv ...
    python -m venv .venv
)

echo [2/2] 正在检查并安装依赖库 ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q

echo [启动] 正在打开桌面控制台 ...
".venv\Scripts\python.exe" start_gui.py
pause
