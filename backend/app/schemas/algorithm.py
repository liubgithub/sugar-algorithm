"""Pydantic schemas for the algorithm API."""
from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field


class AlgorithmInfo(BaseModel):
    id: str
    name: str
    type: str = Field(description="LOCAL or GEE")
    description: str = ""
    params_schema: List[Dict[str, Any]] = Field(default_factory=list)


class AlgorithmListResponse(BaseModel):
    algorithms: List[AlgorithmInfo]