"""SQLite-backed job store.

Stores job records: id, algorithm, params, status, progress, message, result, timestamps.
Phase 1 keeps it small - no migrations framework, just CREATE TABLE IF NOT EXISTS.
"""
import json
import math
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.config import DB_PATH

_lock = threading.Lock()


def _json_safe(value: Any) -> Any:
    """Recursively convert a value into strict-JSON-safe form.

    Algorithms frequently report NaN metrics (e.g. R2 on a single sample).
    json.dumps(allow_nan=True) would persist the invalid `NaN` literal, and
    FastAPI refuses to serialize it back out, breaking GET /api/jobs/{id}.
    Non-finite floats become null; numpy scalars become plain Python types.
    """
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    try:
        import numpy as np
    except ImportError:
        np = None  # type: ignore[assignment]
    if np is not None:
        if isinstance(value, np.floating):
            f = float(value)
            return f if math.isfinite(f) else None
        if isinstance(value, np.integer):
            return int(value)
        if isinstance(value, np.bool_):
            return bool(value)
        if isinstance(value, np.ndarray):
            return [_json_safe(v) for v in value.tolist()]
    return str(value)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_schema() -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                algorithm TEXT NOT NULL,
                params TEXT NOT NULL,
                status TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                result TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # 兼容已有库：旧表没有这些新列；CREATE TABLE IF NOT EXISTS 不会改 schema，
        # 所以这里再加 ALTER，外层 try/except 吞掉"重复列"以兼容新建库。
        for ddl in (
            "ALTER TABLE jobs ADD COLUMN note TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE jobs ADD COLUMN name TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE jobs ADD COLUMN started_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE jobs ADD COLUMN finished_at TEXT NOT NULL DEFAULT ''",
        ):
            try:
                conn.execute(ddl)
                conn.commit()
            except Exception:
                # 列已存在或建表语句已包含；忽略即可。
                pass


def _init_gee_assets_schema() -> None:
    """Persist user-saved GEE Asset references so we can list/use/delete
    them across sessions (the alternative — re-listing via ee.data.listAssets —
    cannot tell the difference between an Asset the user uploaded once and
    a stock EE dataset, and is also capped at ~3 s by the GEE HTTP timeout).

    Each row is one Asset that the user has explicitly chosen to remember.
    The `asset_id` UNIQUE constraint prevents accidental duplicates when the
    same CSV→Asset upload runs twice (the ingest_service already deduplicates
    by file_id + project, but a third-party import path may not).
    """
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gee_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id TEXT NOT NULL UNIQUE,
                algorithm_id TEXT NOT NULL DEFAULT '',
                source_filename TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gee_assets_algo ON gee_assets(algorithm_id)"
        )
        conn.commit()


_init_schema()
_init_gee_assets_schema()


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def create_job(algorithm: str, params: Dict[str, Any], name: Optional[str] = None) -> Dict[str, Any]:
    job_id = uuid.uuid4().hex
    now = _now()
    safe_name = (name or "")[:120]
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO jobs (id, algorithm, params, status, progress, message, result, note, name, started_at, finished_at, created_at, updated_at) "
            "VALUES (?, ?, ?, 'queued', 0, '', '', '', ?, '', '', ?, ?)",
            (
                job_id,
                algorithm,
                json.dumps(_json_safe(params), ensure_ascii=False),
                safe_name,
                now,
                now,
            ),
        )
        conn.commit()
    return get_job(job_id)


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None
    return _row_to_dict(row)


def list_jobs() -> List[Dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
    return [_row_to_dict(r) for r in rows]


def update_job(
    job_id: str,
    *,
    status: Optional[str] = None,
    progress: Optional[int] = None,
    message: Optional[str] = None,
    result: Optional[Dict[str, Any]] = None,
    note: Optional[str] = None,
    name: Optional[str] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
) -> None:
    fields = []
    values: List[Any] = []
    if status is not None:
        fields.append("status = ?")
        values.append(status)
    if progress is not None:
        fields.append("progress = ?")
        values.append(progress)
    if message is not None:
        fields.append("message = ?")
        values.append(message)
    if result is not None:
        fields.append("result = ?")
        values.append(json.dumps(_json_safe(result), ensure_ascii=False))
    if note is not None:
        fields.append("note = ?")
        values.append(note)
    if name is not None:
        fields.append("name = ?")
        values.append(name[:120])
    if started_at is not None:
        fields.append("started_at = ?")
        values.append(started_at)
    if finished_at is not None:
        fields.append("finished_at = ?")
        values.append(finished_at)
    fields.append("updated_at = ?")
    values.append(_now())
    values.append(job_id)

    with _lock, _connect() as conn:
        conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()


def delete_job(job_id: str) -> bool:
    """Delete a job row. Returns True if a row was removed."""
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        conn.commit()
    return cur.rowcount > 0


def cancel_job(job_id: str) -> Optional[str]:
    """Flip status to 'cancelled' only when current status is 'queued'.

    Returns the resulting status string:
      - 'cancelled' on success
      - current status if not cancellable (running / submitted / completed / failed / cancelled)
      - None when the job row does not exist
    """
    with _lock, _connect() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None
        current = row["status"]
        if current != "queued":
            return current
        conn.execute(
            "UPDATE jobs SET status = 'cancelled', finished_at = ?, updated_at = ? WHERE id = ?",
            (_now(), _now(), job_id),
        )
        conn.commit()
    return "cancelled"


# ----------------------------------------------------------------------
# Saved GEE Assets: per-user record of Asset IDs the system has registered
# (e.g. from a successful local-CSV→Asset upload). The row is a *local
# pointer* only — deleting it does NOT touch the Asset in GEE. This matches
# the project invariant in `ingest_service`: the cache maps filenames → Assets
# but never deletes them either.
# ----------------------------------------------------------------------


def list_gee_assets(algorithm_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return saved GEE Assets, newest first.

    ``algorithm_id`` filters by the algorithm that registered the Asset
    (e.g. "time-series-features"). Pass ``None`` to list across algorithms
    (used by the front-end "查看已保存 GEE Assets" UI on the crop_features
    page when the user wants to see everything they've uploaded historically).
    """
    with _connect() as conn:
        if algorithm_id:
            rows = conn.execute(
                "SELECT * FROM gee_assets WHERE algorithm_id = ? ORDER BY created_at DESC, id DESC",
                (algorithm_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM gee_assets ORDER BY created_at DESC, id DESC"
            ).fetchall()
    return [
        {
            "asset_id": r["asset_id"],
            "algorithm_id": r["algorithm_id"],
            "source_filename": r["source_filename"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def save_gee_asset(
    *,
    asset_id: str,
    algorithm_id: str = "",
    source_filename: str = "",
) -> Dict[str, Any]:
    """Insert (or no-op if already present) a saved Asset record.

    Returns the persisted row. A UNIQUE(asset_id) constraint means a re-run
    of the same CSV→Asset upload is a silent no-op — we don't want a new
    row every time the user re-submits the same samples file.
    """
    if not asset_id:
        raise ValueError("asset_id 不能为空")
    now = _now()
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO gee_assets (asset_id, algorithm_id, source_filename, created_at) "
            "VALUES (?, ?, ?, ?)",
            (asset_id, algorithm_id or "", source_filename or "", now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM gee_assets WHERE asset_id = ?", (asset_id,)
        ).fetchone()
    return {
        "asset_id": row["asset_id"],
        "algorithm_id": row["algorithm_id"],
        "source_filename": row["source_filename"],
        "created_at": row["created_at"],
    }


def delete_gee_asset(asset_id: str) -> bool:
    """Delete a saved Asset pointer by full asset id.

    Strictly local — the real Asset on GEE stays untouched. Returns True
    when a row was removed, False when the row was already absent (so a
    double-click on "删除" doesn't crash the page).
    """
    if not asset_id:
        return False
    with _lock, _connect() as conn:
        cur = conn.execute(
            "DELETE FROM gee_assets WHERE asset_id = ?", (asset_id,)
        )
        conn.commit()
    return cur.rowcount > 0


def get_gee_asset(asset_id: str) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM gee_assets WHERE asset_id = ?", (asset_id,)
        ).fetchone()
    if not row:
        return None
    return {
        "asset_id": row["asset_id"],
        "algorithm_id": row["algorithm_id"],
        "source_filename": row["source_filename"],
        "created_at": row["created_at"],
    }


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    keys = row.keys()
    return {
        "job_id": row["id"],
        "algorithm": row["algorithm"],
        "params": json.loads(row["params"]) if row["params"] else {},
        "status": row["status"],
        "progress": row["progress"],
        "message": row["message"],
        "result": json.loads(row["result"]) if row["result"] else {},
        "note": row["note"] if "note" in keys else "",
        "name": row["name"] if "name" in keys else "",
        "started_at": row["started_at"] if "started_at" in keys else "",
        "finished_at": row["finished_at"] if "finished_at" in keys else "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }