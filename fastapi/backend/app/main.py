# -*- coding: utf-8 -*-
"""算法运行平台 FastAPI 入口。

启动（在 backend 目录下）：
    venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routers import algorithms, run

app = FastAPI(
    title="算法运行平台",
    description="选择算法 → 上传/选择参数文件 → 运行 → 展示/下载结果（float 指标、csv 表格、tif 图片）",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(algorithms.router, prefix="/api")
app.include_router(run.router, prefix="/api")


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok"}
