@echo off
cd /d "%~dp0"

REM Install dependencies if needed
pip show PySide6 >nul 2>&1 || (
    echo Installing dependencies...
    pip install -r requirements.txt
)

python mewgenics_manager.py
