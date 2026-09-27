@echo off
REM Start the FastAPI backend on Windows.
cd /d %~dp0
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload