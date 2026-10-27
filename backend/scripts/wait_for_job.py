"""Wait until a job reaches a terminal state, then report it.

Used to babysit a long download from the shell without hammering the API.

Usage from ``backend/``::

    python scripts/wait_for_job.py <job_id> [timeout_seconds]
"""
from __future__ import annotations

import datetime
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import DB_PATH  # noqa: E402


def read_state(job_id: str):
    conn = sqlite3.connect(str(DB_PATH))
    try:
        row = conn.execute(
            "SELECT status, progress, message, result, updated_at "
            "FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()
    return row


def main() -> int:
    job_id = sys.argv[1]
    timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 3600.0
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        row = read_state(job_id)
        if row is None:
            raise SystemExit(f"job not found: {job_id}")
        status, progress, message, _result, updated = row
        head = (message or "").splitlines()[0][:70]
        if (status, progress, head) != last:
            print(
                f"{datetime.datetime.now().strftime('%H:%M:%S')} "
                f"| {status:<9} {progress:>3}% | {head}",
                flush=True,
            )
            last = (status, progress, head)
        if status in ("completed", "failed", "cancelled"):
            print(f"\n终态: {status}")
            return 0
        time.sleep(20)
    print(f"\n超时 {int(timeout)}s，任务仍在 {last[0] if last else '?'}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
