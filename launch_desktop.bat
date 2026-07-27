@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv" (
    echo Virtual environment not found. Run run_app.bat once first to set it up.
    pause
    exit /b 1
)

echo Starting Eagle Drawer Defect Tracker server...
start "Eagle Drawer Defect Tracker Server" cmd /k ".venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000"

timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:8000"
