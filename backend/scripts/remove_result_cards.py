"""删掉某个任务 result.metrics.cards 里的指定指标卡。

用场景：算法源码里去掉了一张卡（例如 "GEE 任务 ID" / "GEE 资产" / "输出文件"
这种内部细节），但历史任务的 result 已经落库了，页面上还是显示老卡片。
这个脚本把历史 result 里的对应卡片摘掉，让页面和现在的算法行为一致。

安全措施：
  * 只改 ``metrics.cards`` 里 key 命中的那几张卡，result 其余字段原样保留；
  * 改之前先把原始 result 备份到 ``data/jobs_backup/<job_id>_<时间戳>.json``；
  * 走应用自己的存储层（``app.storage.job_store.update_job``），和 worker
    写回结果用的是同一条路径。

用法（在 backend/ 目录下执行）::

    python scripts/remove_result_cards.py <job_id> --key gee_task_id,asset_id,output_tif
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.storage import job_store  # noqa: E402

BACKUP_DIR = BACKEND_DIR / "data" / "jobs_backup"


def _backup(job_id: str, result: Dict[str, Any]) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = BACKUP_DIR / f"{job_id}_{stamp}.json"
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="删除任务指标卡")
    parser.add_argument("job_id")
    parser.add_argument(
        "--key",
        required=True,
        help='要删除的卡片 key，逗号分隔，如 "gee_task_id,asset_id,output_tif"',
    )
    args = parser.parse_args()

    keys = {k.strip() for k in args.key.split(",") if k.strip()}
    if not keys:
        raise SystemExit("没有指定任何 key")

    job = job_store.get_job(args.job_id)
    if job is None:
        raise SystemExit(f"找不到任务：{args.job_id}")
    result = job.get("result") or {}
    metrics = result.get("metrics") or {}
    cards: List[Dict[str, Any]] = metrics.get("cards") or []
    if not cards:
        raise SystemExit(f"任务 {args.job_id} 的 result 里没有 metrics.cards")

    keep = [c for c in cards if c.get("key") not in keys]
    removed = [c for c in cards if c.get("key") in keys]
    if not removed:
        raise SystemExit(
            f"任务 {args.job_id} 里没有匹配 {sorted(keys)} 的卡片；"
            f"现有 key：{[c.get('key') for c in cards]}"
        )

    backup = _backup(args.job_id, result)
    metrics["cards"] = keep
    result["metrics"] = metrics
    job_store.update_job(args.job_id, result=result)

    print(f"已删除 {len(removed)} 张卡：" + ", ".join(c.get("key", "?") for c in removed))
    print(f"原 result 备份：{backup}")
    print(f"剩余 {len(keep)} 张卡：" + ", ".join(c.get("key", "?") for c in keep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
