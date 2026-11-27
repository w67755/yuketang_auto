@echo off
cd /d "%~dp0"

echo ==================================================
echo   Yuketang Auto Study Helper (DeepSeek Edition)
echo ==================================================

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.10+.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/2] Creating virtual environment .venv ...
    python -m venv .venv
)

echo [2/2] Checking Python dependencies ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q

echo [RUN] Launching GUI window ...
".venv\Scripts\python.exe" start_gui.py
if errorlevel 1 pause
