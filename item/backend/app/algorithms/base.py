"""Algorithm base interface.

Every algorithm adapter must:
- Provide id, name, type, description, params_schema.
- Implement run(params, job_dir, progress_callback) -> dict.

Run is invoked by the job service in a worker thread for LOCAL algorithms,
and returns immediately with a gee_task_id for GEE algorithms.

The returned dict uses the unified result structure:
{
    "status": "submitted|running|completed|failed",
    "result_type": "raster|csv|json|metrics|gee_task",
    "files": [...],
    "metrics": {...},
    "gee_task_id": None,
    "message": ""
}
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

ProgressCallback = Callable[[int, str], None]


class AlgorithmAdapter(ABC):
    id: str = ""
    name: str = ""
    type: str = "LOCAL"  # LOCAL or GEE
    description: str = ""
    # params_schema is a JSON-Schema-like dict consumed by the frontend
    # to render dynamic forms. Keep it minimal in phase 1.
    params_schema: List[Dict[str, Any]] = []

    @abstractmethod
    def run(
        self,
        params: Dict[str, Any],
        job_dir: str,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        """Execute the algorithm and return a unified result dict."""
        raise NotImplementedError

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "description": self.description,
            "params_schema": self.params_schema,
        }


def make_result(
    *,
    status: str = "completed",
    result_type: str = "json",
    files: Optional[List[Dict[str, Any]]] = None,
    metrics: Optional[Dict[str, Any]] = None,
    gee_task_id: Optional[str] = None,
    message: str = "",
) -> Dict[str, Any]:
    """Helper to build a uniform result payload.

    `metrics` is a free-form dict carrying two kinds of data:
      - `cards`: list[{key, label, value, unit}] — the canonical card format
        the Vue AlgorithmResultPanel reads verbatim to render the tile grid.
      - any other keys (e.g. `county_predictions`, `train_metrics_final`,
        `confusion_matrix`) — algorithm-specific data the panel or a
        dedicated view can branch on.
    """
    return {
        "status": status,
        "result_type": result_type,
        "files": files or [],
        "metrics": metrics or {},
        "gee_task_id": gee_task_id,
        "message": message,
    }


def make_card(key: str, label: str, value: Any, unit: Optional[str] = None) -> Dict[str, Any]:
    """Build one `{key, label, value, unit}` entry for `metrics.cards`.

    `key` is the stable machine identifier (lowercase / underscored, used as
    the dedupe key and React/Vue key). `label` is the user-facing Chinese
    text shown on the card. `unit` may be empty.
    """
    return {
        "key": str(key).strip(),
        "label": str(label).strip(),
        "value": value,
        "unit": unit or "",
    }