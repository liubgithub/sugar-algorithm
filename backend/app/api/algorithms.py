"""Algorithm API: list available algorithms."""
from __future__ import annotations

from typing import Optional

import ee
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.schemas.algorithm import AlgorithmListResponse
from app.services import algorithm_registry as registry_service
from app.services.gee_auth import GeeAuthRequired, initialize_ee
from app.storage import job_store

router = APIRouter(prefix="/api/algorithms", tags=["algorithms"])


# ----------------------------------------------------------------------
# Request / response models for the saved GEE Assets endpoints.
# ----------------------------------------------------------------------


class SavedGeeAsset(BaseModel):
    asset_id: str
    algorithm_id: str = ""
    source_filename: str = ""
    created_at: str


class SavedGeeAssetList(BaseModel):
    assets: list[SavedGeeAsset] = Field(default_factory=list)


class SavedGeeAssetUpsert(BaseModel):
    asset_id: str = Field(min_length=1)
    source_filename: str = ""


class SavedGeeAssetVerify(BaseModel):
    asset_id: str = Field(min_length=1)
    project: Optional[str] = None


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


# ----------------------------------------------------------------------
# Saved GEE Assets: durable list of Assets the user wants to remember.
# These are *local pointers* to GEE Assets. Deleting a row never touches
# the GEE side — the Asset on earthengine.googleapis.com stays exactly
# where it is, by design.
# ----------------------------------------------------------------------


@router.get("/{algorithm_id}/saved-gee-assets", response_model=SavedGeeAssetList)
def list_saved_gee_assets(algorithm_id: str) -> SavedGeeAssetList:
    """List saved GEE Assets, optionally filtered by algorithm.

    The front-end uses this to render the "查看已保存 GEE Assets" list on
    the crop_features form (and any other form that opts in via a
    ``saved_picker`` config block in its ``params_schema``).
    """
    try:
        registry_service.get_algorithm(algorithm_id)
    except KeyError:
        raise HTTPException(
            status_code=404, detail=f"algorithm '{algorithm_id}' not found"
        )
    assets = job_store.list_gee_assets(algorithm_id=algorithm_id)
    return SavedGeeAssetList(assets=[SavedGeeAsset(**a) for a in assets])


@router.post(
    "/{algorithm_id}/saved-gee-assets",
    response_model=SavedGeeAsset,
    status_code=201,
)
def save_gee_asset(algorithm_id: str, body: SavedGeeAssetUpsert) -> SavedGeeAsset:
    """Persist an Asset reference (idempotent on asset_id).

    Called:
      - by ``crop_features`` itself right after a successful
        local-CSV→Asset upload (so the user can re-use it later without
        re-uploading the CSV);
      - by the front-end when the user manually pastes an Asset ID into
        the text input AND opts to "save" it via a future checkbox (kept
        open-ended — the current UI just auto-saves from the algorithm).
    """
    try:
        registry_service.get_algorithm(algorithm_id)
    except KeyError:
        raise HTTPException(
            status_code=404, detail=f"algorithm '{algorithm_id}' not found"
        )
    row = job_store.save_gee_asset(
        asset_id=body.asset_id.strip(),
        algorithm_id=algorithm_id,
        source_filename=body.source_filename,
    )
    return SavedGeeAsset(**row)


@router.delete(
    "/{algorithm_id}/saved-gee-assets/{asset_id:path}",
    response_model=dict,
)
def delete_saved_gee_asset(algorithm_id: str, asset_id: str) -> dict:
    """Delete a saved Asset pointer (NEVER the GEE Asset itself).

    The path matcher ``:path`` lets the asset_id contain its slashes
    (``projects/<p>/assets/<name>``) without escaping.
    """
    try:
        registry_service.get_algorithm(algorithm_id)
    except KeyError:
        raise HTTPException(
            status_code=404, detail=f"algorithm '{algorithm_id}' not found"
        )
    # The asset_id from the URL is already percent-decoded by FastAPI
    # before this handler runs; pass it straight through.
    removed = job_store.delete_gee_asset(asset_id)
    return {"deleted": removed, "asset_id": asset_id}


@router.post(
    "/{algorithm_id}/saved-gee-assets/verify",
    response_model=dict,
)
def verify_gee_asset(algorithm_id: str, body: SavedGeeAssetVerify) -> dict:
    """Best-effort check that ``asset_id`` is queryable on the given project.

    Used by the front-end when the user pastes an Asset ID directly into
    the text input. We don't fail the submission if the asset is
    temporarily slow to respond — the algorithm's run() also re-checks —
    but a clear failure message here lets the user fix typos before
    waiting for a 30-minute GEE task to fail.

    Network / GEE outages collapse to ``{"ok": False, "error": ...}`` with
    HTTP 200 so the form can render the message inline without 500-ing
    the page.
    """
    try:
        registry_service.get_algorithm(algorithm_id)
    except KeyError:
        raise HTTPException(
            status_code=404, detail=f"algorithm '{algorithm_id}' not found"
        )

    project = body.project or "sodium-ray-505904-i3"
    try:
        initialize_ee(project)
    except GeeAuthRequired as exc:
        return {"ok": False, "asset_id": body.asset_id, "error": str(exc)}

    try:
        ee.data.getAsset(body.asset_id)
        return {"ok": True, "asset_id": body.asset_id, "project": project}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "asset_id": body.asset_id,
            "error": (
                f"无法访问 GEE Asset：{body.asset_id}。"
                f"请确认 Asset 存在、属于当前项目 ({project})，且您有读取权限。"
                f"原始错误：{exc}"
            ),
        }