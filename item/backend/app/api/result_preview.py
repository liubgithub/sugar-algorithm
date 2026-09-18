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

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from app.core.config import JOBS_DIR
from app.services import job_service, preview_service

router = APIRouter(prefix="/api/jobs", tags=["result-preview"])

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._\-]+$")
_TIF_EXTS = (".tif", ".tiff")


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