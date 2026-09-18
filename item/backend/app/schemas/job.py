"""Pydantic schemas for the job API."""
from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class JobCreateRequest(BaseModel):
    algorithm: str = Field(description="Algorithm id, e.g. 'demo' or 'yield_local'")
    params: Dict[str, Any] = Field(default_factory=dict)
    name: Optional[str] = Field(
        default=None, max_length=120,
        description="可选任务名；为空时后端按算法 + 时间生成默认名。",
    )


class JobCreateResponse(BaseModel):
    job_id: str
    status: str
    name: str = ""


class JobPatchRequest(BaseModel):
    """Subset of job fields editable via PATCH /api/jobs/{id}.

    当前仅允许修改 `name` 与 `note`；其他字段（status / progress /
    params / result 等）走专门接口，避免前端误改。
    """
    note: Optional[str] = Field(default=None, max_length=500)
    name: Optional[str] = Field(default=None, max_length=120)


class JobCancelResponse(BaseModel):
    job_id: str
    status: str


class JobStatusResponse(BaseModel):
    job_id: str
    algorithm: str
    status: str
    progress: int = 0
    message: str = ""
    note: str = ""
    name: str = ""
    started_at: str = ""
    finished_at: str = ""
    created_at: str = ""
    updated_at: str = ""
    result: Dict[str, Any] = Field(default_factory=dict)