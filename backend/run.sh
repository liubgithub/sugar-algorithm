#!/usr/bin/env bash
# Start the FastAPI backend (Linux/macOS / Git Bash on Windows).
set -e
cd "$(dirname "$0")"
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload