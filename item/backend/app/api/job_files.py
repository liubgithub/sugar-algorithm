"""Job file download API.

Lets the frontend fetch any file written into a job's working directory
(typically the output GeoTIFFs from the algorithm).
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.core.config import JOBS_DIR
from app.services import job_service

router = APIRouter(prefix="/api/jobs", tags=["job-files"])


_SAFE_NAME = re.compile(r"^[A-Za-z0-9._\-]+$")


@router.get("/{job_id}/files")
def list_job_files(job_id: str) -> dict:
    job = job_service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job '{job_id}' not found")
    job_dir = Path(JOBS_DIR) / job_id
    files = []
    if job_dir.exists():
        for p in sorted(job_dir.iterdir()):
            if p.is_file():
                files.append({"name": p.name, "size_bytes": p.stat().st_size})
    return {"job_id": job_id, "files": files}


@router.get("/{job_id}/files/{filename}")
def download_job_file(job_id: str, filename: str):
    job = job_service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job '{job_id}' not found")
    if not _SAFE_NAME.match(filename) or filename.startswith("."):
        raise HTTPException(status_code=400, detail="invalid filename")
    job_dir = Path(JOBS_DIR) / job_id
    file_path = (job_dir / filename).resolve()
    if job_dir.resolve() not in file_path.parents and file_path != job_dir:
        raise HTTPException(status_code=400, detail="path traversal blocked")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"file '{filename}' not found")
    return FileResponse(file_path, filename=filename)