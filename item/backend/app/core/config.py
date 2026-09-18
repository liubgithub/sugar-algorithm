"""Application configuration.

Centralizes paths and runtime settings. Keep MVP simple.
"""
import os
from pathlib import Path

# Project root: backend/app/core/config.py -> backend/
BACKEND_DIR = Path(__file__).resolve().parents[2]
APP_DIR = BACKEND_DIR / "app"
DATA_DIR = BACKEND_DIR / "data"
JOBS_DIR = DATA_DIR / "jobs"
RESULTS_DIR = DATA_DIR / "results"
DB_PATH = DATA_DIR / "sugarcane.db"

# 前端构建产物（vite build 输出），由后端作为静态站点托管。
FRONTEND_DIST_DIR = BACKEND_DIR.parent / "frontend" / "dist"

# Algorithm parameter storage, organized by algorithm id and slot.
# Layout: data/algorithm_data/<algorithm_id>/<slot>/<group_value?>/<files...>
# Replaces the older flat data/uploads/<uuid>/ layout but that directory is
# still kept around for backward compatibility.
ALGO_DATA_DIR = DATA_DIR / "algorithm_data"
UPLOAD_ROOT = DATA_DIR / "uploads"  # legacy flat uploads, retained for back-compat

# Ensure runtime directories exist at import time.
for _p in (DATA_DIR, JOBS_DIR, RESULTS_DIR, ALGO_DATA_DIR, UPLOAD_ROOT):
    _p.mkdir(parents=True, exist_ok=True)

# CORS: open in MVP, restrict later.
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*").split(",")