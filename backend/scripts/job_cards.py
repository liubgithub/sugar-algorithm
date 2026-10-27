"""任务指标卡（metrics.cards）查看 / 修改工具。

VS Code 的 SQLite 扩展是只读的，改不了库。这个脚本提供命令行入口，
让你不用装 GUI 工具也能查、改历史任务的结果。

用法（在 backend/ 目录下执行）：

    # 1. 列出某个算法跑过的任务
    python scripts/job_cards.py list --algorithm yield_local

    # 2. 看某个任务的指标卡
    python scripts/job_cards.py show <job_id>

    # 3. 改单位（"" 表示清空单位）
    python scripts/job_cards.py set-unit <job_id> --key train_mae --unit ""
    python scripts/job_cards.py set-unit <job_id> --key valid_pixel_count --unit 个

    # 4. 批量把某算法所有任务里指定 key 的单位清掉
    python scripts/job_cards.py clear-unit --algorithm yield_local --key train_mae

    # 5. 删除指标卡（逗号分隔可一次删多个）
    python scripts/job_cards.py remove-card <job_id> --key run_mode,folded_history_count

    # 6. 改 result 顶层字段；两个都清空可隐藏前端结果标题 + 灰色副标题
    python scripts/job_cards.py set-field <job_id> --field result_type --value ""
    python scripts/job_cards.py set-field <job_id> --field message --value ""

    # 7. 导出某个任务的结果为可读 JSON（方便在 VS Code 里看）
    python scripts/job_cards.py export <job_id> --out data/job_<id>.json

改库前脚本会自动备份原 result 到 data/jobs_backup/<job_id>_<时间戳>.json，
改错了可以照着备份还原。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Windows 控制台默认 GBK，印不出 "R²" 之类的上标/生僻字会抛
# UnicodeEncodeError；强制切 UTF-8（Python 3.7+）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = BACKEND_DIR / "data" / "sugarcane.db"
BACKUP_DIR = BACKEND_DIR / "data" / "jobs_backup"


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise SystemExit(f"找不到数据库文件：{db_path}")
    return sqlite3.connect(db_path)


def _backup(job_id: str, result_text: str, db_path: Path) -> Path:
    """改动前把原始 result 落一份备份，返回备份路径。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = BACKUP_DIR / f"{job_id}_{stamp}.json"
    path.write_text(result_text, encoding="utf-8")
    return path


def _load_result(con: sqlite3.Connection, job_id: str) -> Optional[Dict[str, Any]]:
    cur = con.cursor()
    cur.execute("SELECT result FROM jobs WHERE id=?", (job_id,))
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"找不到任务：{job_id}")
    if not row[0]:
        raise SystemExit(f"任务 {job_id} 的 result 为空（可能还没跑完）")
    return json.loads(row[0])


def _save_result(
    con: sqlite3.Connection, job_id: str, result: Dict[str, Any], db_path: Path
) -> None:
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    backup = _backup(job_id, text, db_path)
    con.execute("UPDATE jobs SET result=? WHERE id=?", (text, job_id))
    con.commit()
    print(f"已写回 {job_id}；原值备份在 {backup}")


def _get_cards(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    cards = (result.get("metrics") or {}).get("cards")
    return cards if isinstance(cards, list) else []


# ---------------------------------------------------------------- 子命令


def cmd_list(args: argparse.Namespace) -> None:
    con = _connect(args.db)
    cur = con.cursor()
    if args.algorithm:
        cur.execute(
            "SELECT id, status, started_at, finished_at FROM jobs "
            "WHERE algorithm=? ORDER BY started_at DESC",
            (args.algorithm,),
        )
    else:
        cur.execute(
            "SELECT id, status, started_at, finished_at FROM jobs "
            "ORDER BY started_at DESC LIMIT ?",
            (args.limit,),
        )
    rows = cur.fetchall()
    if not rows:
        print("没有匹配的任务。")
    for jid, status, sa, fa in rows:
        tail = f"  完成于 {fa}" if fa else "  （未完成）"
        print(f"{jid}  {status:10s}  {sa}{tail}")
    con.close()


def cmd_show(args: argparse.Namespace) -> None:
    con = _connect(args.db)
    result = _load_result(con, args.job_id)
    con.close()
    cards = _get_cards(result)
    if not cards:
        print("该任务没有 metrics.cards。")
        return
    print(f"任务 {args.job_id} 的指标卡（共 {len(cards)} 张）：")
    for c in cards:
        key = c.get("key", "")
        label = c.get("label", "")
        unit = c.get("unit", "")
        value = c.get("value")
        shown = "（空）" if value is None else value
        unit_shown = f"[{unit}]" if unit else "[]"
        print(f"  {key:38s} {label:14s} {unit_shown:8s} {shown}")


def _patch_unit(
    con: sqlite3.Connection, job_id: str, key: str, unit: str, db_path: Path
) -> None:
    result = _load_result(con, job_id)
    cards = _get_cards(result)
    hits = [c for c in cards if c.get("key") == key]
    if not hits:
        raise SystemExit(f"任务 {job_id} 里没有 key={key} 的卡片")
    for c in hits:
        old = c.get("unit", "")
        c["unit"] = unit
        print(f"  {key}: unit {old!r} -> {unit!r}")
    _save_result(con, job_id, result, db_path)


def cmd_set_unit(args: argparse.Namespace) -> None:
    con = _connect(args.db)
    _patch_unit(con, args.job_id, args.key, args.unit, args.db)
    con.close()


def cmd_remove_card(args: argparse.Namespace) -> None:
    con = _connect(args.db)
    result = _load_result(con, args.job_id)
    metrics = result.get("metrics") or {}
    cards = _get_cards(result)
    keys = {k.strip() for k in args.key.split(",") if k.strip()}
    keep = [c for c in cards if c.get("key") not in keys]
    removed = len(cards) - len(keep)
    if removed == 0:
        con.close()
        raise SystemExit(f"任务 {args.job_id} 里没有匹配 {sorted(keys)} 的卡片")
    metrics["cards"] = keep
    result["metrics"] = metrics
    print(f"  删除 {removed} 张卡：" + ", ".join(
        c.get("key", "?") for c in cards if c.get("key") in keys
    ))
    _save_result(con, args.job_id, result, args.db)
    con.close()


def cmd_set_field(args: argparse.Namespace) -> None:
    """改 result 顶层的任意标量字段（result_type / message 等）。

    前端 ResultRenderer 的 header 条件是 `resultType || hasMessage`，
    把 result_type 和 message 都清空即可让整个 header（含 result_type
    映射出的类型标题和灰色副标题）不渲染。
    """
    con = _connect(args.db)
    result = _load_result(con, args.job_id)
    allowed = {"result_type", "message"}
    if args.field not in allowed:
        con.close()
        raise SystemExit(
            f"只允许改 {sorted(allowed)}（防止误改 status/metrics 等关键字段），收到：{args.field}"
        )
    old = result.get(args.field)
    result[args.field] = args.value
    print(f"  {args.field}: {old!r} -> {args.value!r}")
    _save_result(con, args.job_id, result, args.db)
    con.close()


def cmd_clear_unit(args: argparse.Namespace) -> None:
    con = _connect(args.db)
    cur = con.cursor()
    if args.algorithm:
        cur.execute(
            "SELECT id FROM jobs WHERE algorithm=? AND status='completed'", (args.algorithm,)
        )
    else:
        cur.execute("SELECT id FROM jobs WHERE status='completed'")
    job_ids = [r[0] for r in cur.fetchall()]
    total = 0
    for jid in job_ids:
        result = _load_result(con, jid)
        hits = [c for c in _get_cards(result) if c.get("key") == args.key]
        if not hits:
            continue
        print(f"任务 {jid}:")
        _patch_unit(con, jid, args.key, "", args.db)
        total += len(hits)
    print(f"共处理 {len(job_ids)} 个任务，清掉 {total} 张卡的单位。")
    con.close()


def cmd_export(args: argparse.Namespace) -> None:
    con = _connect(args.db)
    result = _load_result(con, args.job_id)
    con.close()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已导出到 {out}")


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="任务指标卡查看 / 修改工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--db", type=Path, default=DEFAULT_DB, help=f"数据库路径（默认 {DEFAULT_DB}）"
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("list", help="列出任务")
    sp.add_argument("--algorithm", help="按算法名过滤")
    sp.add_argument("--limit", type=int, default=20, help="不带 algorithm 时的条数上限")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("show", help="查看某任务的指标卡")
    sp.add_argument("job_id")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("set-unit", help="改某张卡的单位（'' 表示清空）")
    sp.add_argument("job_id")
    sp.add_argument("--key", required=True, help="卡片 key")
    sp.add_argument("--unit", required=True, help='新单位，"" 表示清空')
    sp.set_defaults(func=cmd_set_unit)

    sp = sub.add_parser("remove-card", help="删除指标卡（--key 传多个用逗号分隔）")
    sp.add_argument("job_id")
    sp.add_argument(
        "--key", required=True, help='要删除的卡片 key，逗号分隔，如 "run_mode,folded_history_count"'
    )
    sp.set_defaults(func=cmd_remove_card)

    sp = sub.add_parser(
        "set-field", help="改 result 顶层字段（仅 result_type / message）"
    )
    sp.add_argument("job_id")
    sp.add_argument("--field", required=True, choices=["result_type", "message"])
    sp.add_argument(
        "--value", required=True, help='新值；传 "" 表示清空（两者都清空可隐藏前端 header）'
    )
    sp.set_defaults(func=cmd_set_field)

    sp = sub.add_parser("clear-unit", help="批量清空某算法所有任务里某个 key 的单位")
    sp.add_argument("--algorithm")
    sp.add_argument("--key", required=True)
    sp.set_defaults(func=cmd_clear_unit)

    sp = sub.add_parser("export", help="把某任务的结果导出成可读 JSON")
    sp.add_argument("job_id")
    sp.add_argument("--out", required=True)
    sp.set_defaults(func=cmd_export)

    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
