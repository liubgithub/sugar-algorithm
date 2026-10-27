"""Result preview API.

Endpoints to inspect a completed job's GeoTIFF outputs without forcing
the browser to parse raw raster data:

  GET /api/jobs/{job_id}/result/files/{filename}/preview.png
  GET /api/jobs/{job_id}/result/files/{filename}/info
  GET /api/jobs/{job_id}/result/files/{filename}/value?lon=&lat=
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from app.core.config import JOBS_DIR
from app.services import job_service, preview_service

router = APIRouter(prefix="/api/jobs", tags=["result-preview"])

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._\-]+$")
_TIF_EXTS = (".tif", ".tiff")

# 一次拼接最多接受多少个文件。上限是为了防止有人构造几百个文件名的请求
# 把后端的拼接管线打满（每个文件都要 open + reproject）。
_MAX_MOSAIC_FILES = 16


def _ensure_tif(job_id: str, filename: str) -> None:
    """Verify job exists, filename is safe, and is a GeoTIFF."""
    job = job_service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job '{job_id}' not found")
    if not _SAFE_NAME.match(filename) or filename.startswith("."):
        raise HTTPException(status_code=400, detail="invalid filename")
    if not filename.lower().endswith(_TIF_EXTS):
        raise HTTPException(status_code=400, detail="only .tif/.tiff is supported for preview")
    job_dir = Path(JOBS_DIR) / job_id
    file_path = (job_dir / filename).resolve()
    if job_dir.resolve() not in file_path.parents:
        raise HTTPException(status_code=400, detail="path traversal blocked")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"file '{filename}' not found in job")


def _parse_filenames(raw: str) -> List[str]:
    """Parse the ``files=a.tif,b.tif`` query parameter into a safe list."""
    names = [part.strip() for part in (raw or "").split(",")]
    names = [n for n in names if n]
    if not names:
        raise HTTPException(status_code=400, detail="missing files")
    if len(names) > _MAX_MOSAIC_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"too many files (max {_MAX_MOSAIC_FILES})",
        )
    # 去重但保持顺序，避免同一个文件被拼两遍。
    seen = set()
    unique: List[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return unique


@router.get("/{job_id}/result/files/{filename}/preview.png")
def preview_png(job_id: str, filename: str):
    _ensure_tif(job_id, filename)
    try:
        png_path = preview_service.get_preview_path(job_id, filename)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"preview generation failed: {exc}") from exc
    if not png_path.is_file():
        raise HTTPException(status_code=500, detail="preview not generated")
    return FileResponse(png_path, media_type="image/png", filename=f"{filename}.png")


@router.get("/{job_id}/result/files/{filename}/info")
def info(job_id: str, filename: str) -> JSONResponse:
    _ensure_tif(job_id, filename)
    try:
        payload = preview_service.get_info(job_id, filename)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"info read failed: {exc}") from exc
    return JSONResponse(payload)


@router.get("/{job_id}/result/files/{filename}/value")
def value(
    job_id: str,
    filename: str,
    lon: float = Query(..., description="Longitude (EPSG:4326)"),
    lat: float = Query(..., description="Latitude (EPSG:4326)"),
    band: int = Query(1, ge=1, description="1-based band index"),
):
    _ensure_tif(job_id, filename)
    try:
        payload = preview_service.pixel_value(job_id, filename, lon, lat, band)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"value lookup failed: {exc}") from exc
    payload["lon"] = lon
    payload["lat"] = lat
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# Mosaic (multi-file) — 多个分片拼成一张完整图
# ---------------------------------------------------------------------------
#
# 有些算法的产物是「同一张图切成若干块」：每块单独一个 .tif，只有拼起来
# 才是完整结果（例如 GEE 把大范围导出拆成多个分片）。这里复用
# preview_service 的 mosaic 管线（展示页上传多张 TIF 时走的同一条路径），
# 让任务结果页也能把多个分片拼成一张图来预览。
#
# 拼接在服务端完成并缓存：合成图的缓存键独立于各分片自己的单图预览，
# 互不覆盖。


@router.get("/{job_id}/result/mosaic/preview.png")
def mosaic_preview_png(
    job_id: str,
    files: str = Query(
        ...,
        description="逗号分隔的文件名，例如 a.tif,b.tif；按 extent 拼成一张",
    ),
):
    names = _parse_filenames(files)
    for name in names:
        _ensure_tif(job_id, name)
    try:
        png_path = preview_service.get_mosaic_preview_path(job_id, names)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"mosaic preview failed: {exc}") from exc
    if not png_path.is_file():
        raise HTTPException(status_code=500, detail="preview not generated")
    return FileResponse(png_path, media_type="image/png", filename="mosaic.png")


@router.get("/{job_id}/result/mosaic/info")
def mosaic_info(
    job_id: str,
    files: str = Query(..., description="逗号分隔的文件名，例如 a.tif,b.tif"),
) -> JSONResponse:
    names = _parse_filenames(files)
    for name in names:
        _ensure_tif(job_id, name)
    try:
        payload = preview_service.get_mosaic_info(job_id, names)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"mosaic info failed: {exc}") from exc
    return JSONResponse(payload)