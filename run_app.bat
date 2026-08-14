@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv" (
    echo Creating virtual environment...
    py -3 -m venv .venv
    if errorlevel 1 (
        python -m venv .venv
    )
)

call .venv\Scripts\activate.bat

echo Installing/upgrading dependencies...
python -m pip install -e ".[dev]" >nul
if errorlevel 1 (
    echo Dependency installation failed. See output above.
    exit /b 1
)

if not exist ".env" (
    copy .env.example .env >nul
)

echo Applying database migrations...
python -m alembic upgrade head
if errorlevel 1 (
    echo Database migration failed. See output above.
    exit /b 1
)

echo.
echo Starting Eagle Drawer Defect Tracker at http://0.0.0.0:8000
echo Reachable on the shop network at http://<this-machine-ip>:8000
echo Press Ctrl+C to stop.
echo.
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
