#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "Installing/upgrading dependencies..."
pip install -e ".[dev]" > /dev/null

if [ ! -f ".env" ]; then
    cp .env.example .env
fi

echo "Applying database migrations..."
python -m alembic upgrade head

echo
echo "Starting Eagle Drawer Defect Tracker at http://127.0.0.1:8000"
echo "Press Ctrl+C to stop."
echo
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
