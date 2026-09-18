"""Algorithm API: list available algorithms."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.schemas.algorithm import AlgorithmListResponse
from app.services import algorithm_registry as registry_service

router = APIRouter(prefix="/api/algorithms", tags=["algorithms"])


@router.get("", response_model=AlgorithmListResponse)
def list_algorithms() -> AlgorithmListResponse:
    algorithms = registry_service.list_algorithms()
    return AlgorithmListResponse(algorithms=algorithms)


@router.get("/{algorithm_id}")
def get_algorithm(algorithm_id: str) -> dict:
    try:
        adapter = registry_service.get_algorithm(algorithm_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"algorithm '{algorithm_id}' not found")
    return adapter.to_dict()