"""GeoTIFF preview + statistics + per-pixel value lookup.

Reads a produced GeoTIFF from a job's working directory, detects its
type (continuous / binary / categorical), generates a PNG preview,
computes statistics, and exposes a single-pixel value lookup.

Design goals:
  * Browser never touches raw GeoTIFF.
  * Heavy work (read + classify + render) happens once per cache cycle.
  * Memory is bounded via an LRU on open rasterio datasets.

This module is read-only with respect to the algorithm outputs - it
never writes back to the source GeoTIFF.
"""
from __future__ import annotations

import functools
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import rasterio
from rasterio.io import DatasetReader
from rasterio.warp import transform_bounds

from app.core.config import JOBS_DIR

# Bounds are always reported in EPSG:4326 so the frontend can safely feed
# them into ol/proj's fromLonLat. ds.bounds returns coordinates in the
# dataset's native CRS (commonly EPSG:3857 or a Chinese projected CRS for
# yield maps), which would land the overlay in the wrong place.
WGS84 = "EPSG:4326"

# Use a non-interactive matplotlib backend; we only write PNGs.
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import colors as mcolors  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_PREVIEW_PX = 1024  # longest edge of the rendered preview PNG
CACHE_SIZE = 8         # number of open rasterio datasets to keep

# 任何对 info.json / preview.png 生成逻辑的破坏性变更都必须 bump 这个版本号，
# 否则 get_info 会直接返回旧 JSON、get_preview_path 会直接返回旧 PNG，
# 新代码永远跑不到。bump 后旧的 .info.json 会被忽略，触发 analyze 重新生成。
# v3: 支持算法写入的「分类元数据元数据」sidecar（<file>.metadata.json），
# 让 categorical 栅格按业务语义（如 10=甘蔗、20=水稻）上色 + 打标签，而不是
# 使用通用 "类别 N" 占位。
# v4: 支持 mosaic 多文件 + 唯一值自定义颜色。前端上传 N 张 TIF 后端用
# rasterio.merge 拼成一张；唯一值模式下用户可传入 custom_colors JSON 覆盖
# 默认调色板。analyze 签名改为 analyze(job_id, filenames: List[str], ...)。
INFO_SCHEMA_VERSION = 4

# Discrete palette for categorical maps; falls back if more classes than entries.
_DISCRETE_PALETTES = [
    ("tab10", 10),
    ("tab20", 20),
    ("tab20b", 20),
    ("tab20c", 20),
]


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _job_dir(job_id: str) -> Path:
    return Path(JOBS_DIR) / job_id


def _preview_dir(job_id: str) -> Path:
    return Path(JOBS_DIR) / job_id / "preview"


def _info_path(job_id: str, filename: str) -> Path:
    return _preview_dir(job_id) / f"{filename}.info.json"


def _png_path(job_id: str, filename: str) -> Path:
    return _preview_dir(job_id) / f"{filename}.png"


def _classification_metadata_path(job_id: str, filename: str) -> Path:
    """Sidecar JSON that algorithms can write next to a categorical GeoTIFF
    to specify per-class labels + colors. When present, ``analyze`` will
    use it instead of the generic auto-assigned colors / "类别 N" labels.

    Schema (all fields optional; missing values fall back to defaults):

        {
          "class_labels": {"10": "甘蔗", "20": "水稻", ...},
          "class_colors": {"10": "#16a34a", "20": "#eab308", ...}
        }

    Keys may be strings or ints (JSON object keys are always strings on
    disk); we coerce to int internally.
    """
    return _job_dir(job_id) / f"{filename}.metadata.json"


# ---------------------------------------------------------------------------
# Class-metadata sidecar
# ---------------------------------------------------------------------------

def load_class_metadata(job_id: str, filename: str) -> Dict[int, Dict[str, str]]:
    """Read the per-job class metadata sidecar if present.

    Returns a ``{int_value: {"label": str, "color": "#rrggbb"}}`` mapping.
    Missing / malformed / empty sidecar files all collapse to ``{}`` so
    the caller can use the result unconditionally without try/except noise.
    """
    sidecar = _classification_metadata_path(job_id, filename)
    if not sidecar.is_file():
        return {}
    try:
        with sidecar.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}

    out: Dict[int, Dict[str, str]] = {}
    raw_labels = data.get("class_labels") if isinstance(data.get("class_labels"), dict) else {}
    raw_colors = data.get("class_colors") if isinstance(data.get("class_colors"), dict) else {}

    # JSON object keys are always strings — coerce keys to int when possible.
    # Keep only entries whose key is a valid int.
    for key, label in raw_labels.items():
        try:
            cls_int = int(key)
        except (TypeError, ValueError):
            continue
        entry: Dict[str, str] = {}
        if isinstance(label, str) and label.strip():
            entry["label"] = label.strip()
        color = raw_colors.get(key)
        if isinstance(color, str) and color.strip():
            entry["color"] = color.strip()
        if entry:
            out[cls_int] = entry
    return out


# ---------------------------------------------------------------------------
# Dataset cache
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=CACHE_SIZE)
def _open_dataset(job_id: str, filename: str) -> DatasetReader:
    """Open (and cache) a rasterio dataset for a job's GeoTIFF."""
    path = _job_dir(job_id) / filename
    return rasterio.open(path)


def clear_cache(job_id: Optional[str] = None) -> None:
    """Drop cached datasets. The job_id arg is kept for caller readability;
    functools.lru_cache 没有按 key 单独淘汰的能力，这里只能整张表清掉。"""
    _open_dataset.cache_clear()


# ---------------------------------------------------------------------------
# Type detection
# ---------------------------------------------------------------------------

def _detect_type(arr: np.ndarray) -> str:
    """Classify a single-band raster array.

    * binary       : uint8 with values in {0,1} or {0,255}
    * categorical  : integer dtype with <=32 unique values
    * continuous   : everything else
    """
    flat = arr.ravel()
    flat = flat[np.isfinite(flat)] if np.issubdtype(arr.dtype, np.floating) else flat

    if arr.dtype == np.uint8:
        unique = np.unique(flat)
        if unique.size <= 2 and (
            set(unique.tolist()).issubset({0, 1})
            or set(unique.tolist()).issubset({0, 255})
        ):
            return "binary"

    if np.issubdtype(arr.dtype, np.integer):
        unique = np.unique(flat)
        if unique.size <= 32:
            return "categorical"

    return "continuous"


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def _stats_continuous(arr: np.ndarray, nodata: Optional[float]) -> Dict[str, Any]:
    if nodata is not None:
        mask = arr == nodata
    else:
        mask = ~np.isfinite(arr) if np.issubdtype(arr.dtype, np.floating) else np.zeros_like(arr, dtype=bool)
    valid = arr[~mask]
    if valid.size == 0:
        return {"min": None, "max": None, "mean": None, "std": None,
                "p2": None, "p98": None, "nodata_count": int(arr.size)}
    finite = valid[np.isfinite(valid)] if np.issubdtype(valid.dtype, np.floating) else valid
    return {
        "min": float(np.min(finite)),
        "max": float(np.max(finite)),
        "mean": float(np.mean(finite)),
        "std": float(np.std(finite)),
        "p2": float(np.percentile(finite, 2)),
        "p98": float(np.percentile(finite, 98)),
        "nodata_count": int(mask.sum()),
    }


def _stats_binary(arr: np.ndarray, nodata: Optional[float]) -> Dict[str, Any]:
    mask = (arr == nodata) if nodata is not None else np.zeros_like(arr, dtype=bool)
    valid = arr[~mask]
    count_1 = int((valid == 1).sum())
    count_0 = int((valid == 0).sum())
    # Treat {0, 255} as foreground=255 too.
    if count_0 == 0 and count_1 == 0:
        count_255 = int((valid == 255).sum())
        count_0 = int((valid == 0).sum()) + int((valid == 255).sum())  # not actually counted, see below
        count_1 = count_255
        count_0 = 0
    return {
        "count_0": count_0,
        "count_1": count_1,
        "total_valid": int(valid.size),
        "nodata_count": int(mask.sum()),
    }


def _stats_categorical(
    arr: np.ndarray,
    nodata: Optional[float],
    class_metadata: Optional[Dict[int, Dict[str, str]]] = None,
) -> Tuple[Dict[int, int], Dict[int, str]]:
    """Return (counts, class_color_hex) for each unique value in ``arr``.

    ``class_metadata`` is an optional ``{int_value: {"label", "color"}}``
    sidecar produced by algorithms like crop_classification. When provided,
    the algorithm-supplied color (if any) wins over the generic
    ``_pick_color`` palette. The metadata does NOT restrict which classes
    appear — every unique value in the raster still gets a row in the
    legend, just with the algorithm-supplied color when available.
    """
    mask = (arr == nodata) if nodata is not None else np.zeros_like(arr, dtype=bool)
    valid = arr[~mask]
    unique, counts = np.unique(valid, return_counts=True)
    counts_dict: Dict[int, int] = {int(u): int(c) for u, c in zip(unique, counts)}
    classes: Dict[int, str] = {}
    for i, u in enumerate(unique):
        cls_int = int(u)
        meta = (class_metadata or {}).get(cls_int) or {}
        if isinstance(meta.get("color"), str) and meta["color"].startswith("#"):
            classes[cls_int] = meta["color"]
        else:
            classes[cls_int] = _pick_color(int(i), len(unique))
    return counts_dict, classes


def _pick_color(idx: int, total: int) -> str:
    """Pick a hex color for a categorical class."""
    # Walk palettes until we have enough distinct entries.
    cumulative = 0
    for name, size in _DISCRETE_PALETTES:
        cmap = plt.get_cmap(name)
        if total <= cumulative + size:
            local = idx - cumulative
            # If idx is beyond palette size, wrap.
            local = local % size
            rgba = cmap(local / max(size - 1, 1))
            return mcolors.to_hex(rgba)
        cumulative += size
    # Fallback: HSV ramp.
    rgba = plt.get_cmap("hsv")(idx / max(total, 1))
    return mcolors.to_hex(rgba)


# ---------------------------------------------------------------------------
# PNG rendering
# ---------------------------------------------------------------------------

def _downsample_shape(height: int, width: int, max_edge: int = MAX_PREVIEW_PX) -> Tuple[int, int]:
    scale = min(max_edge / max(height, 1), max_edge / max(width, 1), 1.0)
    new_h = max(1, int(round(height * scale)))
    new_w = max(1, int(round(width * scale)))
    return new_h, new_w


def _render_continuous(arr: np.ndarray, stats: Dict[str, Any], out_path: Path, nodata: Optional[float]) -> None:
    vmin, vmax = stats.get("p2"), stats.get("p98")
    # P2/P98 可能退化（估产 P99=P100=0 的常见场景），逐级回退：
    #   1) stats.p2 / p98 —— 99% 区间，但可能相等
    #   2) stats.min / max —— 全局极值
    #   3) 0..1 —— 最后兜底，保证 Normalize 一定不抛错
    if vmin is None or vmax is None or vmin == vmax:
        vmin, vmax = stats.get("min"), stats.get("max")
    if vmin is None or vmax is None or vmin == vmax:
        vmin, vmax = (0.0, 1.0)

    norm = mcolors.Normalize(vmin=vmin, vmax=vmax, clip=True)
    cmap = plt.get_cmap("viridis")
    rgba = cmap(norm(arr))

    if nodata is not None:
        mask = arr == nodata
    else:
        # 无显式 nodata 时，把非有限浮点也视作 nodata，避免 NaN 进入 colormap 变全透明。
        if np.issubdtype(arr.dtype, np.floating):
            mask = ~np.isfinite(arr)
        else:
            mask = np.zeros_like(arr, dtype=bool)

    # 估产图通常只在掩膜内有效，nodata 占绝大部分。若渲染成全透明，
    # 用户看到的几乎全是底图（甚至 CSS #eee），有颜色的像元又被缩到看不见。
    # 这里把 nodata 渲成半透明的浅蓝灰底色，让"估产覆盖范围"显形，
    # 有效像元仍以完整 viridis 色带覆盖在轮廓里。
    rgba[mask, :3] = 0.70          # 偏冷的浅灰（区别于 CSS #eee，避免融成一片）
    rgba[mask, 3] = 0.55           # 55% 不透明，跟 OSM 底图叠加后仍清晰
    rgba[~mask, 3] = 1.0           # 有效像元完全不透明

    plt.imsave(out_path, rgba, format="png")


def _render_nodata_warning(out_path: Path, message: str = "No effective pixels") -> None:
    """Write a placeholder PNG with an explanatory caption when the result
    raster has effectively no data (e.g. estimate output masked to zero).

    Only invoked when effective_pixel_ratio < 5%; size is fixed so the
    cache footprint is bounded. The caption uses ASCII so the renderer
    never depends on a CJK font being installed.
    """
    fig, ax = plt.subplots(figsize=(8, 4), dpi=64)
    ax.set_facecolor("#f1f5f9")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.text(
        0.5, 0.55, message,
        ha="center", va="center",
        fontsize=18, color="#475569",
        transform=ax.transAxes,
    )
    ax.text(
        0.5, 0.30,
        "(possible: outside mask / model had no valid output / previous stage failed)",
        ha="center", va="center",
        fontsize=11, color="#94a3b8",
        transform=ax.transAxes,
    )
    fig.savefig(out_path, format="png", bbox_inches="tight", pad_inches=0.2)
    plt.close(fig)


def _render_binary(arr: np.ndarray, out_path: Path, nodata: Optional[float]) -> None:
    h, w = arr.shape
    rgba = np.zeros((h, w, 4), dtype=np.float32)
    # Foreground = 1 (or 255) -> opaque black; 0 -> transparent
    fg_mask = (arr == 1) | (arr == 255)
    rgba[..., 0] = 0.0
    rgba[..., 1] = 0.0
    rgba[..., 2] = 0.0
    rgba[..., 3] = fg_mask.astype(np.float32)

    if nodata is not None:
        rgba[arr == nodata, 3] = 0.0

    plt.imsave(out_path, rgba, format="png")


def _render_categorical(arr: np.ndarray, classes: Dict[int, str], out_path: Path, nodata: Optional[float]) -> None:
    h, w = arr.shape
    rgba = np.zeros((h, w, 4), dtype=np.float32)
    for cls_value, hex_color in classes.items():
        rgb = mcolors.to_rgb(hex_color)
        mask = arr == cls_value
        rgba[mask, 0] = rgb[0]
        rgba[mask, 1] = rgb[1]
        rgba[mask, 2] = rgb[2]
        rgba[mask, 3] = 1.0
    if nodata is not None:
        rgba[arr == nodata, 3] = 0.0
    plt.imsave(out_path, rgba, format="png")


# ---------------------------------------------------------------------------
# Unique-value rendering (mode="unique")
# ---------------------------------------------------------------------------

# 超过这个数量的唯一值就视为"无法按 unique 上色"，回退到连续模式。
# 32 是分类调色板（tab10 + tab20）的总容量上限；再大就没有可区分的颜色。
_UNIQUE_FALLBACK_THRESHOLD = 32
# 浮点 unique 值四舍五入到的小数位：1 位小数足以把 1.001/1.002 等"伪唯一"合并。
_UNIQUE_FLOAT_DECIMALS = 1


def _round_for_unique(arr: np.ndarray) -> np.ndarray:
    """Reduce spurious unique entries (e.g. 1.0000001 vs 1.0000002)。"""
    if np.issubdtype(arr.dtype, np.floating):
        return np.round(arr.astype(np.float64), decimals=_UNIQUE_FLOAT_DECIMALS)
    return arr


def _nodata_mask(arr: np.ndarray, nodata: Optional[float]) -> np.ndarray:
    if nodata is not None:
        return arr == nodata
    if np.issubdtype(arr.dtype, np.floating):
        return ~np.isfinite(arr)
    return np.zeros_like(arr, dtype=bool)


def _render_unique(
    arr: np.ndarray,
    nodata: Optional[float],
    out_path: Path,
    class_metadata: Optional[Dict[int, Dict[str, str]]] = None,
    custom_colors: Optional[Dict[str, str]] = None,
    unique_values: Optional[List[Any]] = None,
) -> Optional[Tuple[Dict[int, str], Dict[int, Dict[str, Any]], Dict[str, Any], int, int]]:
    """Render by unique value: treat every distinct value as its own class.

    Color priority per class: ``custom_colors`` (user-supplied, JSON keys are
    strings) > ``class_metadata`` (algorithm sidecar) > ``_pick_color``
    (matplotlib categorical palette fallback).

    Returns (render_classes, classes_meta, stats, total_pixels, nodata_pixels)
    on success. Returns None when there are too many unique values — caller
    should fall back to continuous mode in that case.

    For floating-point rasters, values are first rounded to
    ``_UNIQUE_FLOAT_DECIMALS`` decimal places to collapse floating-point noise
    (1.0000001 → 1.0, etc.). Integer rasters use the raw values directly.

    ``unique_values`` (可选) — 用户手动指定要渲染哪些唯一值，例如 ``[10, 20, 30]``。
    传入后只把落在该集合里的像元视为有效，其它像元（含其他 unique 值）按 nodata
    处理；不传则按数组里实际存在的全部 unique 值渲染。这给"重采样破坏了原
    唯一值 / 用户只想看某几个类"场景留了口子。
    """
    work = _round_for_unique(arr)
    mask = _nodata_mask(work, nodata)
    valid = work[~mask]
    unique_vals = np.unique(valid)
    # 用户手动指定了 unique_values 时，只保留集合内的值，并把集合外的像元按
    # nodata 处理。传错类型 / 传空列表 → 当作"未指定"处理（保留原行为）。
    if unique_values:
        wanted_set: set = set()
        for v in unique_values:
            try:
                wanted_set.add(int(v))
            except (ValueError, TypeError):
                # 浮点/字符串也接受，按 round 后的值入集合。
                try:
                    wanted_set.add(int(round(float(v))))
                except (ValueError, TypeError):
                    continue
        if wanted_set:
            unique_vals = np.array(
                [u for u in unique_vals if int(u) in wanted_set],
                dtype=unique_vals.dtype,
            )
            # 把不在 wanted_set 里的像元也置为 nodata，让它们透明。
            if np.issubdtype(work.dtype, np.integer):
                keep_mask = np.isin(work, list(wanted_set))
            else:
                # 浮点按 round 后的整数比对
                keep_mask = np.isin(np.round(work).astype(np.int64), list(wanted_set))
            mask = mask | (~keep_mask)
            valid = work[~mask]
            unique_vals = np.unique(valid)
    if unique_vals.size > _UNIQUE_FALLBACK_THRESHOLD:
        return None

    # 把每个 unique 值映射到索引 0..N-1，用 _render_categorical 上色。
    # 用 np.searchsorted 而不是逐个赋值，避开浮点 == 的精度问题。
    # arr_for_render 等于 -1 表示 nodata/无效像元，会被 _render_categorical 渲成透明。
    index_arr = np.full(work.shape, -1, dtype=np.int32)
    if unique_vals.size > 0:
        # 把浮点 unique_vals 也离散化到整数索引：先 round，再 unique。
        # round 之后 unique_vals 已经是去重的，直接 enumerate 即可。
        for i, u in enumerate(unique_vals):
            # work 已经被 round 过，这里用 np.isclose 应对可能的 1e-9 误差。
            index_arr[np.isclose(work, u)] = i

    # 分配颜色；优先级 custom_colors > sidecar > _pick_color。
    render_classes: Dict[int, str] = {}
    classes_meta: Dict[int, Dict[str, Any]] = {}
    counts: Dict[int, int] = {}
    class_metadata = class_metadata or {}
    custom_colors = custom_colors or {}
    is_int_dtype = np.issubdtype(work.dtype, np.integer)
    is_float_dtype = np.issubdtype(work.dtype, np.floating)
    for i, u in enumerate(unique_vals):
        color: Optional[str] = None
        # 把"整数类"或"取整后为整数的浮点"都视为可寻址类目；其它浮点不进
        # custom_colors 匹配（避免 0.1 / 0.1000001 这种伪匹配）。
        is_int_valuable = is_int_dtype or (is_float_dtype and float(u).is_integer())
        # 1) 用户在前端临时指定的 custom_colors（key 是 str，因为 JSON 解析后 key 都是 str）。
        if is_int_valuable:
            cc_val = custom_colors.get(str(int(u)))
            if isinstance(cc_val, str) and cc_val.startswith("#") and len(cc_val) >= 4:
                color = cc_val
        # 2) 算法写入的 sidecar metadata。
        if color is None:
            meta = class_metadata.get(int(u)) if is_int_valuable else None
            sidecar_color = (meta or {}).get("color") if isinstance((meta or {}).get("color"), str) and (meta or {})["color"].startswith("#") else None
            if sidecar_color is not None:
                color = sidecar_color
        # 3) 调色板 fallback。
        if color is None:
            color = _pick_color(i, len(unique_vals))
        render_classes[i] = color
        if is_int_dtype:
            value: Any = int(u)
            label = str(int(u))
        else:
            value = float(u)
            label = f"{float(u):.{_UNIQUE_FLOAT_DECIMALS}f}"
        classes_meta[i] = {"color": color, "value": value, "label": label}
        counts[i] = int((np.isclose(valid, u)).sum())

    _render_categorical(index_arr, render_classes, out_path, nodata=-1)

    total_pixels = int(arr.size)
    # nodata 计数按原 arr 算：nodata 字段、非有限浮点都算无效。
    if nodata is not None:
        nodata_pixels = int((arr == nodata).sum())
    elif np.issubdtype(arr.dtype, np.floating):
        nodata_pixels = int((~np.isfinite(arr)).sum())
    else:
        nodata_pixels = 0
    stats: Dict[str, Any] = {
        "class_counts": counts,
        "nodata_count": nodata_pixels,
    }
    return render_classes, classes_meta, stats, total_pixels, nodata_pixels


# ---------------------------------------------------------------------------
# Mosaic (multi-file stitch)
# ---------------------------------------------------------------------------

from rasterio.merge import merge as _rio_merge  # noqa: E402  (本地依赖较重，按需 import)


def _mosaic_rasters(
    job_id: str,
    filenames: List[str],
    max_edge: int = MAX_PREVIEW_PX,
) -> Tuple[np.ndarray, int, int, Any, Optional[float]]:
    """把 job_id 目录下的多张单波段 GeoTIFF 用 rasterio.merge 拼成一张。

    自动处理不同的 extent / CRS / 分辨率（重采样到目标网格），并把结果
    进一步 downsample 到 ``max_edge`` 以内，避免下游 _render_*/_stats_* 函数
    处理巨型数组。

    Returns:
        (arr, height, width, transform, nodata)
        - arr 是 (H, W) 的二维数组，下采样到 max_edge 以内
        - transform 是输出仿射变换
        - nodata 是统一后的 nodata 值；多文件 nodata 不一致时落到 None

    Raises:
        ValueError: 文件为空 / 列表为空 / 任何一张图打不开
    """
    if not filenames:
        raise ValueError("filenames must not be empty")
    datasets = []
    try:
        for fn in filenames:
            ds = _open_dataset(job_id, fn)
            if ds.count < 1:
                raise ValueError(f"{fn}: no bands")
            datasets.append(ds)
        # 关键：分类栅格用 mode 重采样，否则 bilinear 会把 10/20/30 这种离散
        # 类目值插值成 13/14/15 这种伪连续值，破坏唯一值分类。这里"全为整数
        # dtype"就视为分类栅格；只要有一张是浮点就走 bilinear。
        all_int = all(np.issubdtype(ds.dtypes[0], np.integer) for ds in datasets)
        # rasterio.merge.merge 返回 (arr, transform)，arr shape 是 (bands, H, W)
        merged, merged_transform = _rio_merge(datasets, method="first")
        # 取第 1 波段。
        arr_full = merged[0]
        # 统一 nodata：取首个文件；其余若不一致就置 None（让非有限值都视为 nodata）。
        nodata = datasets[0].nodata
        for ds in datasets[1:]:
            if (ds.nodata is None) != (nodata is None) or (
                ds.nodata is not None and nodata is not None and ds.nodata != nodata
            ):
                nodata = None
                break
        # 进一步 downsample 到 max_edge 以内
        h_full, w_full = arr_full.shape
        out_h, out_w = _downsample_shape(h_full, w_full, max_edge=max_edge)
        if (out_h, out_w) != (h_full, w_full):
            scale_h = out_h / h_full
            scale_w = out_w / w_full
            # 用 rasterio.warp.reproject 做精确重采样；如果失败降级到最近邻。
            try:
                from rasterio.warp import reproject, Resampling
                dst = np.empty((out_h, out_w), dtype=arr_full.dtype)
                # 分类栅格用 mode（取众数），保留整数类目；连续栅格用 bilinear 平滑。
                resampling = Resampling.mode if all_int else Resampling.bilinear
                reproject(
                    source=arr_full,
                    destination=dst,
                    src_transform=merged_transform,
                    dst_transform=merged_transform
                        * rasterio.Affine(1.0 / scale_w, 0, 0, 0, 1.0 / scale_h, 0),
                    src_crs=datasets[0].crs,
                    dst_crs=datasets[0].crs,
                    resampling=resampling,
                )
                arr_full = dst
                merged_transform = merged_transform * rasterio.Affine(
                    1.0 / scale_w, 0, 0, 0, 1.0 / scale_h, 0
                )
            except Exception:
                # 兜底：用 numpy 直接切片（不精确但能用）。
                row_idx = (np.linspace(0, h_full - 1, out_h)).astype(int)
                col_idx = (np.linspace(0, w_full - 1, out_w)).astype(int)
                arr_full = arr_full[np.ix_(row_idx, col_idx)]
        return arr_full, out_h, out_w, merged_transform, nodata
    finally:
        # 不要在这里关 ds——_open_dataset 用 lru_cache 缓存，关了下次还要再开。
        # rasterio 在进程退出时会统一清理。
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze(
    job_id: str,
    filenames: List[str],
    mode: str = "continuous",
    custom_colors: Optional[Dict[str, str]] = None,
    unique_values: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """Read one or more GeoTIFFs, classify them, render PNG, write info JSON.

    Args:
        filenames: 一个或多个 job_id 目录下的文件名。单文件时直接分析；多文件
            时先用 rasterio.merge 拼成一张再分析（自动处理不同 extent / CRS
            / 分辨率）。
        mode: ``"continuous"`` (default) — type-aware rendering that uses
            viridis for continuous rasters, black/white for binary, and a
            categorical palette for integer rasters with ≤32 unique values.
            ``"unique"`` — force render-by-unique-value regardless of dtype.
        custom_colors: 用户在前端临时指定的「值 → 颜色」映射，仅在
            ``mode="unique"`` 下生效；key 是字符串（JSON 反序列化结果）。
        unique_values: 用户手动指定要渲染的唯一值集合；仅在 ``mode="unique"``
            下生效。空列表 / None → 按数组实际 unique 值渲染；非空 → 只渲染
            这些值，其他像元按 nodata 处理。

    Returns:
        info dict，包含 ``mosaic`` 和 ``mosaic_count`` 字段标识是否为拼接图。
    """
    if not filenames:
        raise ValueError("filenames must not be empty")
    is_mosaic = len(filenames) > 1
    # 用第一张图作为路径 / bounds 的来源；多张时 PNG 也写到第一张图的路径上。
    primary_filename = filenames[0]

    if is_mosaic:
        # 多文件：用 rasterio.merge 拼接，自动 downsample 到 MAX_PREVIEW_PX。
        arr, out_h, out_w, _transform, nodata = _mosaic_rasters(job_id, filenames)
        # mosaic 模式下没有单张 ds 概念，bounds 近似为合并后的矩形；
        # 这里用所有输入文件的并集（用 merge 输出的 transform 反算）。
        try:
            ds0 = _open_dataset(job_id, primary_filename)
            height, width = ds0.height, ds0.width
        except Exception:
            height, width = out_h, out_w
        # 用 primary file 的 ds 来取 bounds（简化处理；不做真正的 mosaic bounds 计算）
        primary_ds = _open_dataset(job_id, primary_filename)
    else:
        primary_ds = _open_dataset(job_id, primary_filename)
        try:
            height, width = primary_ds.height, primary_ds.width
            out_h, out_w = _downsample_shape(height, width)
            arr = primary_ds.read(1, out_shape=(out_h, out_w), masked=False)
        finally:
            pass
        nodata = primary_ds.nodata

    png_path = _png_path(job_id, primary_filename)
    info_path = _info_path(job_id, primary_filename)
    _preview_dir(job_id).mkdir(parents=True, exist_ok=True)

    # mode="unique" 优先级最高：不论原始 dtype，全部按唯一值上色；
    # 唯一值过多 (>32) 时回退到连续模式，type 字段如实记为"连续"。
    if mode == "unique":
        # mosaic 模式下没有 sidecar（sidecar 是按 filename 命名的），
        # 这里只查 primary 的 sidecar，多文件时副作用可控。
        class_metadata = load_class_metadata(job_id, primary_filename) if not is_mosaic else None
        unique_result = _render_unique(arr, nodata, png_path, class_metadata, custom_colors, unique_values)
        if unique_result is None:
            raster_type = "continuous"
            stats = _stats_continuous(arr, nodata)
            total_pixels = int(arr.size)
            nodata_pixels = int(stats.get("nodata_count") or 0)
            effective = max(total_pixels - nodata_pixels, 0)
            effective_ratio = (effective / total_pixels) if total_pixels else 0.0
            if total_pixels > 0 and effective == 0:
                _render_nodata_warning(
                    png_path,
                    message=f"No effective pixels ({effective}/{total_pixels})",
                )
            else:
                _render_continuous(arr, stats, png_path, nodata)
            classes = None
            result_classes = None
            result_class_metadata = None
        else:
            render_classes, classes_meta, stats, total_pixels, nodata_pixels = unique_result
            raster_type = "unique"
            effective = max(total_pixels - nodata_pixels, 0)
            effective_ratio = (effective / total_pixels) if total_pixels else 0.0
            classes = render_classes
            result_classes = render_classes
            result_class_metadata = classes_meta
    else:
        arr_for_type = primary_ds.read(1, out_shape=(min(height, 256), min(width, 256)), masked=False) if not is_mosaic else arr
        if is_mosaic:
            # mosaic 模式下 primary file 的 dtype 不一定等于 arr 的 dtype；
            # 直接用 arr 探测类型。
            raster_type = _detect_type(arr_for_type)
        else:
            raster_type = _detect_type(arr_for_type)

        if raster_type == "continuous":
            stats = _stats_continuous(arr, nodata)
            total_pixels = int(arr.size)
            nodata_pixels = int(stats.get("nodata_count") or 0)
            effective = max(total_pixels - nodata_pixels, 0)
            effective_ratio = (effective / total_pixels) if total_pixels else 0.0
            if total_pixels > 0 and effective == 0:
                # 真正零有效像元时才用占位图。估产图天然稀疏（耕地 mask
                # 只覆盖部分区域，常见有效率 1%-20%），旧版 5% 阈值会误判，
                # 把本来能看到的产量图替换成 "No effective pixels" 提示。
                _render_nodata_warning(
                    png_path,
                    message=f"No effective pixels ({effective}/{total_pixels})",
                )
            else:
                _render_continuous(arr, stats, png_path, nodata)
            classes = None
            result_classes = None
            result_class_metadata = None
        elif raster_type == "binary":
            stats = _stats_binary(arr, nodata)
            total_pixels = int(arr.size)
            nodata_pixels = int(stats.get("nodata_count") or 0)
            effective = max(total_pixels - nodata_pixels, 0)
            effective_ratio = (effective / total_pixels) if total_pixels else 0.0
            if total_pixels > 0 and effective == 0:
                _render_nodata_warning(png_path)
            else:
                _render_binary(arr, png_path, nodata)
            # Provide a trivial legend (foreground vs background).
            classes = {0: "#ffffff", 1: "#000000"}
            if stats["count_0"] == 0 and stats["count_1"] > 0:
                classes = {0: "#ffffff", 255: "#000000"}
            result_classes = classes
            result_class_metadata = None
        else:
            # 读分类元数据 sidecar（如果算法为该 .tif 写过）。当算法指定了
            # 类别颜色 / 业务标签（10=甘蔗、20=水稻…）时，预览图与 info.json
            # 的 classes 字段会用业务色，否则图片走 _pick_color 的通用色，
            # labels 由前端 ResultRenderer 在 v-for 时回退到『类别 N』占位文案。
            class_metadata = load_class_metadata(job_id, primary_filename) if not is_mosaic else None
            counts, classes = _stats_categorical(arr, nodata, class_metadata)
            total_pixels = int(arr.size)
            nodata_pixels = int((arr == nodata).sum()) if nodata is not None else 0
            effective = max(total_pixels - nodata_pixels, 0)
            effective_ratio = (effective / total_pixels) if total_pixels else 0.0
            if total_pixels > 0 and effective == 0:
                _render_nodata_warning(png_path)
            else:
                _render_categorical(arr, classes, png_path, nodata)
            # classes_dict: 给前端"图例"用 —— 同时携带颜色与业务标签，
            # 算法写过 sidecar 时 labels 非空，否则 labels 为空，前端回退到『类别 N』。
            labels_meta = {k: v.get("label", "") for k, v in (class_metadata or {}).items()}
            classes_dict: Dict[int, Dict[str, str]] = {
                int(cls_value): {
                    "color": hex_color,
                    **({"label": labels_meta[int(cls_value)]} if labels_meta.get(int(cls_value)) else {}),
                }
                for cls_value, hex_color in classes.items()
            }
            stats = {
                "class_counts": counts,
                "nodata_count": nodata_pixels,
            }
            # 同时把 classes 输出成扁平 hex 字典，保持向后兼容（ResultRenderer
            # 当前按 `classes[cls] -> color` 读取）。新的标签 / 颜色字典则用
            # `class_metadata` 字段暴露，供前端 UI 升级后使用。
            result_classes: Any = classes
            result_class_metadata = classes_dict if labels_meta else None

    bounds = primary_ds.bounds  # rasterio.coords.BoundingBox — in the dataset's CRS
    # Normalize to EPSG:4326 so the frontend can safely call fromLonLat.
    # rasterio's ds.bounds returns native CRS coordinates; yield maps are
    # often in EPSG:3857 or a Chinese projected CRS, which would put the
    # overlay canvas in the middle of the ocean if we passed them through
    # fromLonLat unchanged.
    src_crs = primary_ds.crs
    if src_crs is not None and str(src_crs) != WGS84:
        try:
            left, bottom, right, top = transform_bounds(src_crs, WGS84, *bounds)
        except Exception:
            # Fallback: ship native bounds. Frontend will be visibly wrong,
            # but the API still returns a valid response instead of 500.
            left, bottom, right, top = (
                float(bounds.left),
                float(bounds.bottom),
                float(bounds.right),
                float(bounds.top),
            )
    else:
        left, bottom, right, top = (
            float(bounds.left),
            float(bounds.bottom),
            float(bounds.right),
            float(bounds.top),
        )
    info_payload: Dict[str, Any] = {
        "job_id": job_id,
        "filename": primary_filename,
        "filenames": list(filenames),
        "mosaic": is_mosaic,
        "mosaic_count": len(filenames),
        "schema_version": INFO_SCHEMA_VERSION,
        "type": raster_type,
        "width": int(width),
        "height": int(height),
        "preview_width": int(out_w),
        "preview_height": int(out_h),
        "bounds": [
            [float(left), float(bottom)],
            [float(right), float(top)],
        ],
        "stats": stats,
        "classes": classes,
        "class_metadata": (result_class_metadata if raster_type in ("categorical", "unique") else None),
        "cmap": "viridis" if raster_type == "continuous" else None,
        "nodata": float(nodata) if nodata is not None else None,
        "effective_pixel_ratio": float(effective_ratio),
        "effective_pixel_count": int(effective),
        "total_pixel_count": int(total_pixels),
    }
    with info_path.open("w", encoding="utf-8") as fh:
        json.dump(info_payload, fh, ensure_ascii=False, indent=2)

    return info_payload


def get_info(job_id: str, filename: str) -> Dict[str, Any]:
    """Return the cached info dict, generating it on first request.

    旧版本缓存（schema_version 缺失或不匹配）一律视为失效，触发 analyze
    重新生成。这样改阈值、改 bounds CRS 等破坏性变更能立刻生效，无需手动
    清理 .info.json。
    """
    info_path = _info_path(job_id, filename)
    if info_path.is_file():
        try:
            with info_path.open("r", encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("schema_version") == INFO_SCHEMA_VERSION:
                return cached
        except (json.JSONDecodeError, OSError):
            pass
    return analyze(job_id, [filename])


def get_preview_path(job_id: str, filename: str) -> Path:
    """Return PNG path, generating it on first request.

    只要 info 缓存 schema 不匹配就重新生成 PNG（analyze 会顺带写 PNG），
    保证预览图和 info 永远是同一版本的产物。
    """
    info_path = _info_path(job_id, filename)
    cache_valid = False
    if info_path.is_file():
        try:
            with info_path.open("r", encoding="utf-8") as fh:
                cache_valid = json.load(fh).get("schema_version") == INFO_SCHEMA_VERSION
        except (json.JSONDecodeError, OSError):
            cache_valid = False
    if not cache_valid:
        analyze(job_id, [filename])
    return _png_path(job_id, filename)


def pixel_value(job_id: str, filename: str, lon: float, lat: float, band: int = 1) -> Dict[str, Any]:
    """Read the raw pixel value at a geographic coordinate."""
    ds = _open_dataset(job_id, filename)
    # ds.sample returns one row per (x, y) with all bands; select `band`.
    samples = list(ds.sample([(float(lon), float(lat))]))
    if not samples:
        return {"value": None, "type": "unknown", "band": band}
    row = samples[0]
    if band < 1 or band > len(row):
        return {"value": None, "type": "unknown", "band": band}
    raw = row[band - 1]
    info = get_info(job_id, filename)
    raster_type = info.get("type", "continuous")
    nodata = info.get("nodata")
    if raw is None:
        return {"value": None, "type": raster_type, "band": band}
    try:
        if float(raw) == float("nan"):
            return {"value": None, "type": raster_type, "band": band}
    except (TypeError, ValueError):
        pass
    if nodata is not None and float(raw) == float(nodata):
        return {"value": None, "type": raster_type, "band": band}
    # Cast numeric types to plain Python scalars so the JSON response is clean.
    if isinstance(raw, (np.integer,)):
        v: Any = int(raw)
    elif isinstance(raw, (np.floating,)):
        v = float(raw)
    else:
        v = raw
    return {"value": v, "type": raster_type, "band": band}


__all__ = [
    "analyze",
    "get_info",
    "get_preview_path",
    "pixel_value",
    "clear_cache",
]