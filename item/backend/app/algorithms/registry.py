"""Algorithm registry.

Holds all known AlgorithmAdapter instances. New algorithms are added by
importing their module (side-effect import) so the adapter self-registers.

Phase 1: register the demo + six real algorithm adapters (mostly stubs).
"""
from __future__ import annotations

from typing import Dict, List

from app.algorithms.base import AlgorithmAdapter

_registry: Dict[str, AlgorithmAdapter] = {}


def register(adapter: AlgorithmAdapter) -> None:
    if not adapter.id:
        raise ValueError("AlgorithmAdapter must define a non-empty id")
    _registry[adapter.id] = adapter


def get(algorithm_id: str) -> AlgorithmAdapter:
    if algorithm_id not in _registry:
        raise KeyError(f"algorithm '{algorithm_id}' is not registered")
    return _registry[algorithm_id]


def list_all() -> List[AlgorithmAdapter]:
    return list(_registry.values())


def has(algorithm_id: str) -> bool:
    return algorithm_id in _registry


def _bootstrap() -> None:
    # Side-effect imports ensure each adapter self-registers.
    from app.algorithms.yield_local.adapter import YieldLocalAdapter
    from app.algorithms.harvest.adapter import HarvestAdapter
    from app.algorithms.crop_threshold.adapter import CropThresholdAdapter
    from app.algorithms.crop_features.adapter import CropFeaturesAdapter
    from app.algorithms.crop_classification.adapter import CropClassificationAdapter
    from app.algorithms.algorithm_6.adapter import Algorithm6Adapter
    from app.algorithms.demo.adapter import DemoAdapter

    register(YieldLocalAdapter())
    register(HarvestAdapter())
    register(CropThresholdAdapter())
    register(CropFeaturesAdapter())
    register(CropClassificationAdapter())
    register(Algorithm6Adapter())
    register(DemoAdapter())


_bootstrap()