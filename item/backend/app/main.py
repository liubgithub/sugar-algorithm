"""FastAPI application entry point."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import algorithms as algorithms_api
from app.api import job_files as job_files_api
from app.api import jobs as jobs_api
from app.api import result_preview as result_preview_api
from app.api import uploads as uploads_api
from app.core.config import CORS_ORIGINS, FRONTEND_DIST_DIR

# Importing registry triggers the algorithm bootstrap.
from app.algorithms import registry as _registry  # noqa: F401


def create_app() -> FastAPI:
    app = FastAPI(title="Sugarcane Remote Sensing Platform", version="0.2.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(algorithms_api.router)
    app.include_router(jobs_api.router)
    app.include_router(uploads_api.uploads_router)
    app.include_router(uploads_api.datasets_router)
    app.include_router(job_files_api.router)
    app.include_router(result_preview_api.router)

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    # 托管前端构建产物：http://localhost:8000/ 直接打开平台页面。
    # 路由使用 hash 模式（/#/jobs），浏览器只请求 "/" 和 /assets/*，
    # 因此无需额外的 SPA 路径回退。挂载放在所有 API 路由之后。
    if FRONTEND_DIST_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIST_DIR), html=True), name="frontend")

    return app


app = create_app()