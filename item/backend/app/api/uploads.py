"""File upload + algorithm datasets API.

Two responsibilities:

1. Upload user files into the project storage. There are two layouts:

   - **Algorithm-aware** (preferred): `data/algorithm_data/<algorithm_id>/<slot>[/<group_value>]/...`
     Used when the client provides `algorithm` + `slot` form fields. Files are
     scoped to a specific algorithm and parameter slot, which lets later runs
     pick from a per-slot history via the `/api/datasets` endpoints.

   - **Legacy flat**: `data/uploads/<uuid>/...`. Kept for backward compatibility
     so older jobs and existing data keep working. Triggered when no
     `algorithm` / `slot` is supplied.

   Single-file and multi-file (webkitdirectory / zip) uploads both flow through
   the same handler; the only difference is whether the target is a single
   file or a directory.

2. Datasets API for browsing / deleting previously uploaded parameter files,
   organized by algorithm and slot. Used by the "我的数据" page and the inline
   history dropdowns in the algorithm-detail form.
"""
from __future__ import annotations

import shutil
import uuid
import zipfile
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.core.config import ALGO_DATA_DIR, UPLOAD_ROOT
from app.algorithms import registry as algo_registry
from app.services import algorithm_registry as registry_service

# Two routers: one for the upload endpoints (kept at /api/uploads for back-
# compat with the existing frontend), one for the new datasets API.
uploads_router = APIRouter(prefix="/api/uploads", tags=["uploads"])
datasets_router = APIRouter(prefix="/api/datasets", tags=["datasets"])

UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
ALGO_DATA_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _safe_name(name: str) -> str:
    """Strip directory components and keep a printable name."""
    base = Path(name).name
    cleaned = "".join(c if c.isalnum() or c in "._-" else "_" for c in base)
    return cleaned or "upload.bin"


def _safe_dir_name(name: str) -> str:
    """Sanitize a directory name (no slashes, printable)."""
    base = Path(name).name
    cleaned = "".join(c if c.isalnum() or c in "._-" else "_" for c in base)
    return cleaned or "upload"


def _atomic_target(parent: Path, prefix: str, suffix: str, is_dir: bool) -> Path:
    """Build a non-colliding path under parent with format `<prefix>__<suffix>`."""
    target = parent / f"{prefix}__{suffix}"
    if is_dir:
        # For directories, append a numeric suffix if a sibling exists.
        if not target.exists():
            return target
        i = 2
        while True:
            cand = parent / f"{prefix}__{suffix}_{i}"
            if not cand.exists():
                return cand
            i += 1
    else:
        if not target.exists():
            return target
        stem = Path(suffix).stem
        ext = Path(suffix).suffix
        i = 2
        while True:
            cand = parent / f"{prefix}__{stem}_{i}{ext}"
            if not cand.exists():
                return cand
            i += 1


def _dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _validate_algorithm(algorithm_id: Optional[str]) -> Optional[str]:
    """If algorithm_id is set, ensure it is registered. Returns normalized id."""
    if not algorithm_id:
        return None
    aid = algorithm_id.strip()
    if not aid:
        return None
    if not algo_registry.has(aid):
        raise HTTPException(status_code=400, detail=f"unknown algorithm: {aid}")
    # Use the registered id (lowercased) as the on-disk namespace.
    return registry_service.get_algorithm(aid).id


# ---------------------------------------------------------------------------
# POST /api/uploads
# ---------------------------------------------------------------------------


@uploads_router.post("")
async def upload_file(
    file: Optional[UploadFile] = File(None),
    files: Optional[List[UploadFile]] = File(None),
    algorithm: Optional[str] = Form(None),
    slot: Optional[str] = Form(None),
    group_value: Optional[str] = Form(None),
    label: Optional[str] = Form(None),
) -> dict:
    """Upload one or more files.

    Form fields:
      - file:   a single file (legacy behavior)
      - files:  multiple files (used by webkitdirectory / multi-select)
      - algorithm:   algorithm id (e.g. "yield_local"); routes into the
                     algorithm-aware layout when provided.
      - slot:        parameter slot within that algorithm (e.g. "excel",
                     "rasters", "mask"). Required when `algorithm` is set.
      - group_value: optional sub-grouping key (e.g. "target_2026-06").
      - label:       optional user-friendly name shown in "我的数据".
    """
    upload_list = files or ([file] if file else None)
    if not upload_list:
        raise HTTPException(status_code=400, detail="missing file(s)")

    algo_id = _validate_algorithm(algorithm)
    if algo_id and not slot:
        raise HTTPException(
            status_code=400,
            detail="slot is required when algorithm is set",
        )

    # Dedup: when FastAPI exposes the same upload under both `file` and
    # `files`, keep a single copy. We dedup by id(file) so duplicates of the
    # same UploadFile object don't get written twice.
    seen = set()
    deduped = []
    for f in upload_list:
        if f is None:
            continue
        if id(f) in seen:
            continue
        seen.add(id(f))
        deduped.append(f)
    upload_list = deduped or upload_list

    file_id = uuid.uuid4().hex[:12]

    # ----- Choose target directory -----
    if algo_id and slot:
        # Algorithm-aware layout.
        target_dir = ALGO_DATA_DIR / algo_id / slot
        if group_value:
            safe_group = _safe_dir_name(group_value)
            target_dir = target_dir / safe_group
        target_dir.mkdir(parents=True, exist_ok=True)
    else:
        # Legacy flat layout (kept for back-compat).
        target_dir = UPLOAD_ROOT / file_id
        target_dir.mkdir(parents=True, exist_ok=True)

    # ----- Branch A: legacy single-file (no algorithm scoping) -----
    if len(upload_list) == 1 and not algo_id:
        return await _save_single_legacy(upload_list[0], file_id, target_dir, label)

    # ----- Branch B: algorithm-aware single file (kind="file" slot) -----
    if len(upload_list) == 1 and algo_id:
        return await _save_single_algo_aware(upload_list[0], file_id, target_dir, algo_id, slot, group_value, label)

    # ----- Branch C: multi-file (webkitdirectory / multi-select) -----
    saved_entries: List[dict] = []
    has_relpath = any(getattr(f, "webkitRelativePath", "") for f in upload_list if f)
    for f in upload_list:
        if not f or not f.filename:
            continue
        safe = _safe_name(f.filename)
        rel = getattr(f, "webkitRelativePath", "") or ""
        if rel and has_relpath:
            rel_dir = Path(rel).parent
            dest_dir = target_dir / rel_dir
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_path = dest_dir / safe
        else:
            dest_path = target_dir / safe
        with dest_path.open("wb") as out:
            while True:
                chunk = await f.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        saved_entries.append({"name": safe, "path": str(dest_path), "size_bytes": dest_path.stat().st_size})

    if has_relpath and algo_id:
        # Preserve the user's directory structure (e.g. target_2026-06/foo.tif).
        return _dataset_response(
            file_id, algo_id, slot, group_value, label, "directory",
            str(target_dir), _dir_size(target_dir), target_dir.name
        )

    # Loose multi-file (no webkitRelativePath): wrap into <file_id>__bundle/
    wrapper = _atomic_target(target_dir, file_id, "bundle", is_dir=True)
    wrapper.mkdir(parents=True, exist_ok=True)
    for entry in saved_entries:
        shutil.move(entry["path"], wrapper / Path(entry["path"]).name)
    return _dataset_response(
        file_id, algo_id, slot, group_value, label, "directory",
        str(wrapper), _dir_size(wrapper), wrapper.name
    )


async def _save_single_algo_aware(
    f: UploadFile,
    file_id: str,
    target_dir: Path,
    algo_id: str,
    slot: str,
    group_value: Optional[str],
    label: Optional[str],
) -> dict:
    """Single-file upload scoped to a specific algorithm slot.

    Saves as a regular file. If the file is a .zip, auto-extracts it as a
    directory payload (matching the legacy behavior).
    """
    if not f.filename:
        raise HTTPException(status_code=400, detail="missing filename")
    safe = _safe_name(f.filename)
    saved_path = target_dir / safe
    size_bytes = 0
    with saved_path.open("wb") as out:
        while True:
            chunk = await f.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            size_bytes += len(chunk)
    if safe.lower().endswith(".zip"):
        extract_dir = _atomic_target(target_dir, file_id, _safe_dir_name(Path(safe).stem), is_dir=True)
        extract_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(saved_path, "r") as zf:
                zf.extractall(extract_dir)
                # If the zip has a single top-level directory, flatten it so the
                # algorithm sees the files directly. This matches the common
                # case where the user zips a "target_2026-06/" folder.
                top_entries = list(extract_dir.iterdir())
                if (len(top_entries) == 1
                        and top_entries[0].is_dir()
                        and not any(p.name.startswith("__MACOSX") for p in top_entries)):
                    inner = top_entries[0]
                    for child in inner.iterdir():
                        shutil.move(str(child), str(extract_dir / child.name))
                    shutil.rmtree(inner, ignore_errors=True)
        except zipfile.BadZipFile as exc:
            shutil.rmtree(extract_dir, ignore_errors=True)
            saved_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=f"invalid zip: {exc}")
        saved_path.unlink(missing_ok=True)
        size_bytes = _dir_size(extract_dir)
        return _dataset_response(
            file_id, algo_id, slot, group_value, label, "directory",
            str(extract_dir), size_bytes, extract_dir.name
        )
    # Plain file: rename to <file_id>__<safe> for stable per-upload naming.
    final_path = _atomic_target(target_dir, file_id, safe, is_dir=False)
    if final_path != saved_path:
        saved_path.rename(final_path)
        saved_path = final_path
    return _dataset_response(
        file_id, algo_id, slot, group_value, label, "file",
        str(saved_path), size_bytes, safe
    )


async def _save_single_legacy(f: UploadFile, file_id: str, target_dir: Path, label: Optional[str]) -> dict:
    """Legacy single-file path: writes to data/uploads/<uuid>/<name>."""
    if not f.filename:
        raise HTTPException(status_code=400, detail="missing filename")
    safe = _safe_name(f.filename)
    saved_path = target_dir / safe
    size_bytes = 0
    with saved_path.open("wb") as out:
        while True:
            chunk = await f.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            size_bytes += len(chunk)
    kind = "file"
    payload_path: Path = saved_path
    if safe.lower().endswith(".zip"):
        kind = "directory"
        extract_dir = target_dir / "extracted"
        extract_dir.mkdir(exist_ok=True)
        try:
            with zipfile.ZipFile(saved_path, "r") as zf:
                zf.extractall(extract_dir)
                # Flatten single top-level directory if present.
                top_entries = list(extract_dir.iterdir())
                if (len(top_entries) == 1
                        and top_entries[0].is_dir()
                        and not any(p.name.startswith("__MACOSX") for p in top_entries)):
                    inner = top_entries[0]
                    for child in inner.iterdir():
                        shutil.move(str(child), str(extract_dir / child.name))
                    shutil.rmtree(inner, ignore_errors=True)
        except zipfile.BadZipFile as exc:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise HTTPException(status_code=400, detail=f"invalid zip: {exc}")
        payload_path = extract_dir
        size_bytes = _dir_size(extract_dir)
    return {
        "file_id": file_id,
        "algorithm_id": None,
        "slot": None,
        "group_value": None,
        "label": label,
        "name": safe,
        "kind": kind,
        "path": str(payload_path),
        "size_bytes": size_bytes,
    }


def _dataset_response(
    file_id: str,
    algo_id: Optional[str],
    slot: Optional[str],
    group_value: Optional[str],
    label: Optional[str],
    kind: str,
    path: str,
    size_bytes: int,
    name: str,
) -> dict:
    return {
        "file_id": file_id,
        "algorithm_id": algo_id,
        "slot": slot,
        "group_value": _safe_dir_name(group_value) if group_value else None,
        "label": label,
        "name": name,
        "kind": kind,
        "path": path,
        "size_bytes": size_bytes,
    }


# ---------------------------------------------------------------------------
# Legacy debug endpoint (kept for back-compat with anything already calling it).
# ---------------------------------------------------------------------------


@uploads_router.get("")
def list_uploads() -> dict:
    items = []
    if UPLOAD_ROOT.exists():
        for entry in sorted(UPLOAD_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not entry.is_dir():
                continue
            items.append({
                "file_id": entry.name,
                "is_dir": any(p.is_file() for p in entry.rglob("*")),
            })
    return {"uploads": items}


# ---------------------------------------------------------------------------
# Datasets API
# ---------------------------------------------------------------------------


def _algo_root(algo_id: str) -> Path:
    return ALGO_DATA_DIR / algo_id


def _slot_root(algo_id: str, slot: str) -> Path:
    return ALGO_DATA_DIR / algo_id / slot


def _slot_uses_group_by(algo_id: str, slot: str) -> bool:
    """Return True if the algorithm's schema declares `group_by` for this slot."""
    try:
        adapter = registry_service.get_algorithm(algo_id)
    except KeyError:
        return False
    for field in adapter.params_schema or []:
        if field.get("slot") == slot and field.get("group_by"):
            return True
    return False


def _scan_slot_files(slot_dir: Path) -> List[dict]:
    """Return one entry per top-level file or directory under slot_dir.

    Files/dirs whose name starts with `_` are skipped (reserved for metadata).
    Convention: a per-upload bundle has the form `<file_id>__<name>`.
    """
    if not slot_dir.exists():
        return []
    entries = []
    for child in sorted(slot_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if child.name.startswith("_"):
            continue
        if child.is_dir():
            file_id, _, _ = child.name.partition("__")
            entries.append({
                "file_id": file_id,
                "name": child.name.split("__", 1)[-1],
                "kind": "directory",
                "path": str(child),
                "size_bytes": _dir_size(child),
                "created_at": child.stat().st_mtime,
            })
        else:
            file_id, _, _ = child.name.partition("__")
            entries.append({
                "file_id": file_id,
                "name": child.name.split("__", 1)[-1],
                "kind": "file",
                "path": str(child),
                "size_bytes": child.stat().st_size,
                "created_at": child.stat().st_mtime,
            })
    return entries


@datasets_router.get("")
def list_datasets() -> dict:
    """List every algorithm's slots, with file counts and total size."""
    algorithms = []
    if ALGO_DATA_DIR.exists():
        for algo_dir in sorted(ALGO_DATA_DIR.iterdir()):
            if not algo_dir.is_dir():
                continue
            slot_summaries = []
            for slot_dir in sorted(algo_dir.iterdir()):
                if not slot_dir.is_dir() or slot_dir.name.startswith("_"):
                    continue
                flat_files = _scan_slot_files(slot_dir)
                group_values = []
                if _slot_uses_group_by(algo_dir.name, slot_dir.name):
                    for sub in sorted(slot_dir.iterdir()):
                        if not sub.is_dir():
                            continue
                        sub_files = _scan_slot_files(sub)
                        if sub_files:
                            group_values.append({
                                "group_value": sub.name,
                                "file_count": len(sub_files),
                                "size_bytes": sum(f["size_bytes"] for f in sub_files),
                            })
                slot_summaries.append({
                    "slot": slot_dir.name,
                    "file_count": len(flat_files),
                    "size_bytes": sum(f["size_bytes"] for f in flat_files),
                    "group_values": group_values,
                })
            try:
                adapter = registry_service.get_algorithm(algo_dir.name)
                name = adapter.name
            except KeyError:
                name = algo_dir.name
            algorithms.append({
                "id": algo_dir.name,
                "name": name,
                "slots": slot_summaries,
            })
    return {"algorithms": algorithms}


@datasets_router.get("/{algorithm_id}")
def get_algorithm_datasets(algorithm_id: str) -> dict:
    """Return one algorithm's slots with full file listings.

    For slots with sub-directories (group_value layout) we expose each
    group_value as its own entry so the UI can offer them as separate
    selections (e.g. target_2026-06 vs target_2026-05).
    """
    root = _algo_root(algorithm_id)
    try:
        name = registry_service.get_algorithm(algorithm_id).name
    except KeyError:
        name = algorithm_id
    if not root.exists():
        return {"id": algorithm_id, "name": name, "slots": []}

    slots = []
    for slot_dir in sorted(root.iterdir()):
        if not slot_dir.is_dir() or slot_dir.name.startswith("_"):
            continue
        slot_entry = {"slot": slot_dir.name, "groups": []}
        # If the schema declares group_by for this slot, the children of
        # slot_dir are group_value subdirs (one per upload group). Otherwise
        # they are flat per-upload entries.
        if _slot_uses_group_by(algorithm_id, slot_dir.name):
            for sub in sorted(slot_dir.iterdir()):
                if not sub.is_dir():
                    continue
                files = _scan_slot_files(sub)
                if files:
                    slot_entry["groups"].append({
                        "group_value": sub.name,
                        "files": files,
                    })
        else:
            slot_entry["groups"].append({
                "group_value": None,
                "files": _scan_slot_files(slot_dir),
            })
        slots.append(slot_entry)
    return {"id": algorithm_id, "name": name, "slots": slots}


@datasets_router.delete("/{algorithm_id}/{slot}/{file_id}")
def delete_dataset(algorithm_id: str, slot: str, file_id: str) -> dict:
    """Delete a single file or directory from an algorithm's slot."""
    slot_dir = _slot_root(algorithm_id, slot)
    if not slot_dir.exists():
        raise HTTPException(status_code=404, detail="slot not found")
    # Safety: file_id is hex; reject anything weird so we can't escape the slot.
    if not file_id.replace("-", "").replace("_", "").isalnum() or len(file_id) > 32:
        raise HTTPException(status_code=400, detail="invalid file id")
    removed = None
    for entry in slot_dir.iterdir():
        if entry.name.startswith("_"):
            continue
        if entry.name.startswith(file_id + "__"):
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            removed = entry.name
            break
    # Also search one level deeper (group_value layout).
    if removed is None:
        for sub in slot_dir.iterdir():
            if not sub.is_dir():
                continue
            for entry in sub.iterdir():
                if entry.name.startswith(file_id + "__"):
                    if entry.is_dir():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink(missing_ok=True)
                    removed = f"{sub.name}/{entry.name}"
                    break
            if removed:
                break
    if removed is None:
        raise HTTPException(status_code=404, detail="file not found")
    return {"deleted": removed}