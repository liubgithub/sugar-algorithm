"""Job API: create jobs, query job status, edit metadata, cancel queued jobs."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas.job import (
    JobCancelResponse,
    JobCreateRequest,
    JobCreateResponse,
    JobPatchRequest,
    JobStatusResponse,
)
from app.services import job_service
from app.services import algorithm_registry as registry_service

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.post("", response_model=JobCreateResponse)
def create_job(req: JobCreateRequest) -> JobCreateResponse:
    try:
        registry_service.get_algorithm(req.algorithm)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"unknown algorithm: {req.algorithm}")

    job = job_service.create_job(req.algorithm, req.params, name=req.name)
    return JobCreateResponse(
        job_id=job["job_id"], status=job["status"], name=job.get("name", "")
    )


@router.get("/{job_id}", response_model=JobStatusResponse)
def get_job(job_id: str) -> JobStatusResponse:
    job = job_service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job '{job_id}' not found")
    return JobStatusResponse(
        job_id=job["job_id"],
        algorithm=job["algorithm"],
        status=job["status"],
        progress=job["progress"],
        message=job["message"],
        note=job.get("note", ""),
        name=job.get("name", ""),
        started_at=job.get("started_at", ""),
        finished_at=job.get("finished_at", ""),
        created_at=job.get("created_at", ""),
        updated_at=job.get("updated_at", ""),
        result=job["result"],
    )


@router.patch("/{job_id}", response_model=JobStatusResponse)
def patch_job(job_id: str, req: JobPatchRequest) -> JobStatusResponse:
    """Edit a job's mutable fields (currently: name + note).

    status / progress / params / result 等字段不允许通过 PATCH 改；
    取消走 POST /{id}/cancel，结果由 worker 写入。
    """
    if job_service.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail=f"job '{job_id}' not found")
    if req.note is not None:
        job_service.update_job_note(job_id, req.note)
    if req.name is not None:
        job_service.update_job_name(job_id, req.name)
    return get_job(job_id)


@router.delete("/{job_id}")
def delete_job(job_id: str) -> dict:
    """Remove a job record plus its working directory.

    Allowed even for running jobs in the MVP; the worker thread continues
    writing until it next touches the deleted path.
    """
    if not job_service.delete_job(job_id):
        raise HTTPException(status_code=404, detail=f"job '{job_id}' not found")
    return {"job_id": job_id, "deleted": True}


@router.post("/{job_id}/cancel", response_model=JobCancelResponse)
def cancel_job(job_id: str) -> JobCancelResponse:
    """取消一个尚未运行的任务。

    只允许取消 `queued` 状态；其它状态一律返回 409 + 当前状态。
    第一阶段不强制终止 running 任务，worker 线程按既有逻辑继续跑。
    """
    new_status = job_service.cancel_job(job_id)
    if new_status is None:
        raise HTTPException(status_code=404, detail=f"job '{job_id}' not found")
    if new_status != "cancelled":
        raise HTTPException(
            status_code=409,
            detail=f"job '{job_id}' is '{new_status}', only queued jobs can be cancelled",
        )
    return JobCancelResponse(job_id=job_id, status="cancelled")


@router.get("/{job_id}/log")
def get_job_log(job_id: str, offset: int = Query(0, ge=0)) -> dict:
    """Tail the algorithm's captured stdout, starting at a byte offset."""
    log = job_service.get_job_log(job_id, offset)
    if log is None:
        raise HTTPException(status_code=404, detail=f"job '{job_id}' not found")
    return log


@router.get("")
def list_jobs() -> dict:
    return {"jobs": job_service.list_jobs()}