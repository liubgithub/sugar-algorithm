"""Diagnose "Invalid GeoJSON geometry." on a GEE Table asset.

只读诊断脚本：定位某个 GEE Table Asset 里到底是哪几个要素的几何无法被 GEE 解析。

用法（从 backend/ 目录）::

    python scripts/diag_gee_asset_geometry.py \
        --project united-crane-506207-e5 \
        --asset "研究区=projects/united-crane-506207-e5/assets/guangxi_shp" \
        --asset "特征文件=projects/united-crane-506207-e5/assets/Guangxi_Crop_Features2_Tezheng_2025"

原理
----
``fc.geometry().getInfo()`` 这类调用会把整表的几何序列化回客户端；只要有一个要素的
几何非法，GEE 就整条拒绝并只回一句 "Invalid GeoJSON geometry."，不告诉你是哪个。
但按 ``system:index`` 过滤后的子集是独立求值的，所以可以二分：

    probe(subset) 成功  → 子集内几何全部合法
    probe(subset) 失败  → 子集内至少有一个坏要素，继续对半切

复杂度 O(k·log n) 次请求（k = 坏要素个数），受 ``--max-probes`` 限制。
每个坏要素再用 ``aggregate_array(<属性>).get(0)`` 把它的属性（class / 名称等）
取出来 —— 该路径不碰几何，所以对坏要素也能取到，方便用户在源数据里定位。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

# Windows console defaults to GBK, which can't encode the ✓/✗ marks below.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.gee_auth import initialize_ee  # noqa: E402


PROBE_BUDGET = 60


def _probe(fc: Any, idxs: List[str]) -> Optional[Exception]:
    """Return the exception if this index subset fails to serialize, else None."""
    import ee

    try:
        fc.filter(ee.Filter.inList("system:index", idxs)).geometry().getInfo()
        return None
    except Exception as exc:  # noqa: BLE001
        return exc


def _find_bad_indexes(fc: Any, idxs: List[str], budget: int) -> Tuple[List[str], Optional[Exception], int]:
    """Bisect ``idxs`` for the subset(s) whose geometry GEE refuses to serialize."""
    first_err = _probe(fc, idxs)
    used = 1
    if first_err is None:
        return [], None, used

    bad: List[str] = []
    stack: List[List[str]] = [idxs]
    while stack and used < budget:
        cur = stack.pop()
        if len(cur) == 1:
            bad.append(cur[0])
            continue
        mid = len(cur) // 2
        left, right = cur[:mid], cur[mid:]
        # 先探一半；失败就继续切这一半，成功再探另一半。
        err_left = _probe(fc, left)
        used += 1
        if err_left is not None:
            if len(left) == 1:
                bad.append(left[0])
            else:
                stack.append(left)
            continue
        if used >= budget:
            break
        err_right = _probe(fc, right)
        used += 1
        if err_right is not None:
            if len(right) == 1:
                bad.append(right[0])
            else:
                stack.append(right)
    return bad, first_err, used


def _props_of(fc: Any, idx: str) -> Optional[dict]:
    """Best-effort: read a bad feature's properties without touching geometry."""
    import ee

    try:
        names = fc.first().propertyNames().getInfo()
    except Exception:  # noqa: BLE001
        return None
    one = fc.filter(ee.Filter.eq("system:index", idx))
    out = {}
    for p in names or []:
        if p == "system:index":
            continue
        try:
            out[p] = one.aggregate_array(p).get(0).getInfo()
        except Exception:  # noqa: BLE001
            out[p] = "<取不到>"
    return out


def diagnose(label: str, asset_id: str) -> int:
    import ee

    print(f"\n{'=' * 72}\n[{label}] {asset_id}\n{'=' * 72}")
    try:
        meta = ee.data.getAsset(asset_id)
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 连 Asset 元数据都读不到: {exc}")
        return 1
    print(f"  type = {meta.get('type')}  bbox = {meta.get('bbox')}")

    fc = ee.FeatureCollection(asset_id)
    try:
        n = fc.size().getInfo()
        print(f"  ✓ size() = {n} 个要素（该调用不碰几何，所以能过不代表几何合法）")
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ size() 失败: {exc}")
        return 1

    try:
        fc.geometry().getInfo()
        print("  ✓ geometry() 正常 —— 几何整体可解析，问题不在这个 Asset")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ geometry() 失败: {exc}")

    try:
        idxs = fc.aggregate_array("system:index").getInfo()
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 取 system:index 失败，无法二分定位: {exc}")
        return 1
    idxs = [str(i) for i in idxs]
    print(f"  … 共 {len(idxs)} 个要素，开始二分定位坏要素（最多 {PROBE_BUDGET} 次请求）")

    t0 = time.monotonic()
    bad, err, used = _find_bad_indexes(fc, idxs, PROBE_BUDGET)
    print(f"  → {used} 次探测，{time.monotonic() - t0:.1f}s，定位到 {len(bad)} 个坏要素")
    if not bad:
        print("  ! 二分未能定位（可能探测预算用尽，或问题不集中在少数要素上）")
        return 1
    for idx in bad[:20]:
        props = _props_of(fc, idx)
        print(f"    ✗ system:index={idx}  属性: {props}")
    if len(bad) > 20:
        print(f"    …… 另有 {len(bad) - 20} 个坏要素未列出")
    print(f"  原始 GEE 错误: {err}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument(
        "--asset",
        action="append",
        required=True,
        help="标签=asset_id，可重复。标签可省略，如 projects/x/assets/y",
    )
    ap.add_argument("--max-probes", type=int, default=PROBE_BUDGET)
    args = ap.parse_args()

    initialize_ee(args.project)
    rc = 0
    for spec in args.asset:
        label, _, asset_id = spec.partition("=")
        if not asset_id:
            label, asset_id = asset_id or spec, spec
        rc |= diagnose(label.strip(), asset_id.strip())
    return rc


if __name__ == "__main__":
    raise SystemExit(main())