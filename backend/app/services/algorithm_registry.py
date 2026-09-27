"""Service layer for the algorithm registry.

Thin wrapper over the registry module so the API layer never imports
app.algorithms.registry directly.
"""
from __future__ import annotations

from typing import List

from app.algorithms import registry as algo_registry
from app.algorithms.base import AlgorithmAdapter


def list_algorithms() -> List[dict]:
    return [a.to_dict() for a in algo_registry.list_all()]


def get_algorithm(algorithm_id: str) -> AlgorithmAdapter:
    return algo_registry.get(algorithm_id)