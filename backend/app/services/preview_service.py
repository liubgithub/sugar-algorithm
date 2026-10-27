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
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import rasterio
from rasterio.io import DatasetReader
from rasterio.transform import Affine, from_origin
from rasterio.warp import Resampling, reproject, transform_bounds

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
# v5: mosaic 改成「先算 union bounds + 目标分辨率，再直接输出 ≤1024 的小图」，
# 不再先拼整幅数组（5 G 像元 → 5 GB 内存）。合成图用独立缓存键，不再覆盖
# 第一个文件自己的单图预览。目标 CRS 固定 WGS84，各源 CRS 不同也能拼。
# v6: 修正 mosaic 的几何 —— 窗口偏移 / 尺寸按整数吸附（消除浮点噪声导致的
# 差一个像元），输出网格用 round 而不是 ceil（与单图预览一致），bounds 取
# 各源真实 union extent 而不是 ceil 出来的网格。分片拼回来的结果与原图
# 完全对齐。旧缓存的 bounds / 预览尺寸是错的，版本号升到 6 让它们失效重建。
# v7: info.json 里记录源文件的 (mtime_ns, size) 签名。缓存命中时必须同时
# 匹配 schema_version **和** 签名 —— 否则同名结果文件被重新下载 / 重新发布
# （例如任务重跑、GEE 结果重新拉取）之后，服务端还会拿旧文件的预览图和
# 图例，把已经修好的结果继续显示成坏图。
# v8: 签名里再加上分类元数据 sidecar 的签名 —— sidecar 决定图例的类名和
# 颜色，手工改过 sidecar（比如把"甘蔗"调成红色）之后预览必须跟着重建。
INFO_SCHEMA_VERSION = 8

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

def _file_signature(job_id: str, filename: str) -> Tuple[int, int]:
    """``(mtime_ns, size)`` of a job file — its "version" on disk.

    同一个路径下的结果文件可能被整体换掉（重新下载、重新发布、切分分片
    覆盖原文件），这时旧的数据集句柄和旧的预览缓存都必须作废。用文件签名
    当版本号比用路径当版本号可靠。
    """
    try:
        st = (_job_dir(job_id) / filename).stat()
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def _source_signature(job_id: str, filenames: List[str]) -> List[List[int]]:
    """Signatures of every input that fed a preview (one entry per file).

    每个文件两项：栅格本身的 ``(mtime_ns, size)`` ＋ 分类元数据 sidecar 的
    ``(mtime_ns, size)``。sidecar 决定图例里的类名和颜色，用户在 IDE 里
    直接改 sidecar 也必须让缓存失效 —— 否则图例一直显示旧颜色，而用户
    明明刚把"甘蔗"改成红色。
    """
    out: List[List[int]] = []
    for fn in filenames:
        raster = list(_file_signature(job_id, fn))
        sidecar = list(_sidecar_signature(job_id, fn))
        out.append(raster + sidecar)
    return out


def _sidecar_signature(job_id: str, filename: str) -> Tuple[int, int]:
    """``(mtime_ns, size)`` of a file's class-metadata sidecar (0,0 if none)."""
    try:
        st = _classification_metadata_path(job_id, filename).stat()
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


@functools.lru_cache(maxsize=CACHE_SIZE)
def _open_dataset(job_id: str, filename: str, signature: Tuple[int, int]) -> DatasetReader:
    """Open (and cache) a rasterio dataset for a job's GeoTIFF.

    ``signature`` 是文件在打开那一刻的 ``(mtime_ns, size)``，并且是缓存 key
    的一部分：文件被换掉之后签名就变了 → key 变了 → 重新 open，而不是拿
    着旧句柄去读新文件（Windows 上旧句柄还会挡住 os.replace）。
    """
    path = _job_dir(job_id) / filename
    return rasterio.open(path)


def _get_dataset(job_id: str, filename: str) -> DatasetReader:
    """Open the dataset for ``filename``, reusing the cache when unchanged."""
    return _open_dataset(job_id, filename, _file_signature(job_id, filename))


def clear_cache(job_id: Optional[str] = None) -> None:
    """Drop cached datasets. The job_id arg is kept for caller readability;
    functools.lru_cache 没有按 key 单独淘汰的能力，这里只能整张表清掉。"""
    _open_dataset.cache_clear()


def _cache_is_valid(cached: Any, job_id: str, filenames: List[str]) -> bool:
    """True when a cached info payload still describes the files on disk.

    两道关：schema_version（逻辑格式）和 source_signature（文件有没有被
    换掉）。只查前者的话，任务重跑 / 结果重新下载之后会一直返回旧预览。
    """
    if not isinstance(cached, dict):
        return False
    if cached.get("schema_version") != INFO_SCHEMA_VERSION:
        return False
    return cached.get("source_signature") == _source_signature(job_id, filenames)


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


def _mosaic_cache_key(filenames: List[str]) -> str:
    """合成图预览的缓存键。

    合成图必须和「单文件预览」分开缓存：旧实现把合成 PNG 写到第一个文件名
    对应的路径下，一拼接就把那张图自己的单图预览冲掉了，用户来回切换时
    图会莫名其妙地变。文件名排序后再哈希，保证同一组文件永远命中同一个键。
    """
    digest = hashlib.md5(",".join(sorted(filenames)).encode("utf-8")).hexdigest()[:12]
    return f"mosaic_{digest}"


def _mosaic_class_metadata(
    job_id: str, filenames: List[str]
) -> Dict[int, Dict[str, str]]:
    """合并多个文件的分类元数据 sidecar（先出现的优先）。

    分类算法会给每个分片各写一份 ``<file>.metadata.json``；合成图要显示
    业务标签（10=甘蔗 / 20=水稻…）就得把各分片的 sidecar 合并起来，否则
    图例会退化成「类别 10 / 类别 20」。
    """
    merged: Dict[int, Dict[str, str]] = {}
    for fn in filenames:
        for key, value in load_class_metadata(job_id, fn).items():
            merged.setdefault(key, value)
    return merged


def _snap_int(value: float, tol: float = 1e-5) -> float:
    """Snap a value that should be a whole number onto that number.

    浮点噪声会让本该是整数的比值变成 119.99999999999832 这种值。直接
    ``ceil`` 会多出一行/一列（分片之间因此重叠一行，"先来的赢" 会把
    相邻分片的一行覆盖掉）；直接 ``floor`` 又会少一行（那一行就成了空洞）。
    这里把「本该是整数」的值吸附回整数，误差在 1e-5 个像元以内才吸附。
    """
    nearest = round(value)
    if abs(value - nearest) < tol:
        return float(nearest)
    return value


def _mosaic_rasters(
    job_id: str,
    filenames: List[str],
    max_edge: int = MAX_PREVIEW_PX,
) -> Tuple[np.ndarray, int, int, Any, Optional[float], Tuple[float, float, float, float]]:
    """把 job_id 目录下的多张单波段 GeoTIFF 拼成一张**小图**。

    与旧实现的区别（这是必须改的地方）::

        旧：rasterio.merge 拼出整幅数组 → 再 downsample 到 1024
        新：先算 union bounds + 目标分辨率 → 直接输出 ≤ max_edge 的小图

    广西 10 m 全幅是 84 720 × 59 251 = 5.02 G 像元，uint8 拼整幅就是 5 GB
    内存 —— 旧实现在这种规模上会直接把后端进程打爆。新实现的峰值内存
    ~ max_edge² × 8 B（8 MB 量级），与数据规模无关。

    目标 CRS 固定 EPSG:4326：
      * 各源 CRS 不同也能拼（``rasterio.merge`` 要求所有源同 CRS，跨 CRS
        会直接抛错）；
      * info 里的 bounds 与前端 ``fromLonLat`` 的口径一致，不用再转一次。

    重叠区域按「先来的赢」（与 ``rasterio.merge(method="first")`` 一致）：
    合成图的分片本来就是同一张图切开的，正常情况下不重叠。

    Returns:
        (arr, height, width, transform, nodata, extent)
        - arr 是 (H, W) 数组，H/W ≤ max_edge
        - transform 是输出仿射变换（EPSG:4326）
        - nodata 是统一后的 nodata 值；各源 nodata 不一致时落到 None
        - extent 是各源 union bounds（左、下、右、上，WGS84 度）。注意这是
          **数据**的真实范围，而不是输出网格的范围 —— 网格是 ceil 出来的，
          末行/末列可能比数据多出不到一个像元。报真实范围才能让拼接图和
          单图预览的 bounds 完全一致（分片拼回来应该等于原图）。

    Raises:
        ValueError: 文件列表为空 / 任何一张图没有波段 / 源缺少 CRS
    """
    if not filenames:
        raise ValueError("filenames must not be empty")

    datasets = []
    for fn in filenames:
        ds = _get_dataset(job_id, fn)
        if ds.count < 1:
            raise ValueError(f"{fn}: no bands")
        if ds.crs is None:
            raise ValueError(f"{fn}: 缺少 CRS，无法参与拼接")
        datasets.append(ds)

    # --- 1. 各源在 WGS84 下的范围 ---
    bounds_list = []
    for ds in datasets:
        b = ds.bounds
        if str(ds.crs) != WGS84:
            try:
                tb = transform_bounds(ds.crs, WGS84, b.left, b.bottom, b.right, b.top)
            except Exception:
                tb = (b.left, b.bottom, b.right, b.top)
        else:
            tb = (b.left, b.bottom, b.right, b.top)
        bounds_list.append(tb)

    left = min(b[0] for b in bounds_list)
    bottom = min(b[1] for b in bounds_list)
    right = max(b[2] for b in bounds_list)
    top = max(b[3] for b in bounds_list)
    width_deg = max(right - left, 1e-12)
    height_deg = max(top - bottom, 1e-12)

    # --- 2. 目标分辨率 ---
    # 取「各源原生分辨率的最大值」和「塞进 max_edge 所需的分辨率」中较大者：
    # 既不会把数据放大插值，又保证输出边长不超过 max_edge。
    native_res = 0.0
    for ds, tb in zip(datasets, bounds_list):
        w_deg = max(tb[2] - tb[0], 1e-12)
        h_deg = max(tb[3] - tb[1], 1e-12)
        native_res = max(
            native_res, w_deg / max(ds.width, 1), h_deg / max(ds.height, 1)
        )
    native_res = native_res or 1.0
    fit_res = max(width_deg / max_edge, height_deg / max_edge)
    res = max(native_res, fit_res)

    # 输出网格的边长用 round 而不是 ceil：ceil 会多出不到一整行/一列，
    # 让合成图和单图预览的网格尺寸对不上（1024x717 vs 1024x716），拼出来的
    # 图和原图看着就不是同一张。round 最多丢掉半个像元的边缘，和单图预览
    # 的行为一致。
    out_w = max(1, min(max_edge, int(round(_snap_int(width_deg / res)))))
    out_h = max(1, min(max_edge, int(round(_snap_int(height_deg / res)))))
    dst_transform = from_origin(left, top, res, res)

    # --- 3. 逐源重采样到同一张目标网格 ---
    all_int = all(np.issubdtype(ds.dtypes[0], np.integer) for ds in datasets)
    # 累加器用 int32 + 哨兵 -9999（不在 10/20/30/40/50 这些真实类目里）；
    # 连续图用 float64 + 哨兵 -1e30，最后统一转成 NaN。
    acc_dtype = np.int32 if all_int else np.float64
    sentinel = -9999 if all_int else -1e30
    acc = np.full((out_h, out_w), sentinel, dtype=acc_dtype)
    filled = np.zeros((out_h, out_w), dtype=bool)

    # 降采样倍率 ≤ 16 时用 mode（取众数，保住整数码类目，10/20/30 不会
    # 变成 13/14/15）；倍率再大时 GDAL 的 mode 要扫全量源像元，慢到不可用，
    # 退回 nearest —— nearest 同样是「取原值」，不会造出伪类目。
    reduction = res / native_res
    if all_int:
        resampling = Resampling.mode if reduction <= 16 else Resampling.nearest
    else:
        resampling = Resampling.bilinear

    for ds, tb in zip(datasets, bounds_list):
        col_off = int(math.floor(_snap_int((tb[0] - left) / res)))
        row_off = int(math.floor(_snap_int((top - tb[3]) / res)))
        win_w = int(math.ceil(_snap_int((tb[2] - tb[0]) / res)))
        win_h = int(math.ceil(_snap_int((tb[3] - tb[1]) / res)))
        col_off = max(col_off, 0)
        row_off = max(row_off, 0)
        win_w = min(win_w, out_w - col_off)
        win_h = min(win_h, out_h - row_off)
        if win_w <= 0 or win_h <= 0:
            continue
        win_transform = dst_transform * Affine.translation(col_off, row_off)
        buf = np.full((win_h, win_w), sentinel, dtype=acc_dtype)
        reproject(
            source=rasterio.band(ds, 1),
            destination=buf,
            src_transform=ds.transform,
            src_crs=ds.crs,
            dst_transform=win_transform,
            dst_crs=WGS84,
            src_nodata=ds.nodata,
            dst_nodata=sentinel,
            resampling=resampling,
            init_dest_nodata=True,
        )
        valid = buf != sentinel
        region = acc[row_off:row_off + win_h, col_off:col_off + win_w]
        region_mask = filled[row_off:row_off + win_h, col_off:col_off + win_w]
        take = valid & ~region_mask
        region[take] = buf[take]
        region_mask[take] = True

    # --- 4. 收尾：哨兵 → nodata，dtype 还原成源的 dtype ---
    nodata_values = {ds.nodata for ds in datasets}
    src_nodata = nodata_values.pop() if len(nodata_values) == 1 else None
    if all_int:
        # 还原成源的整数 dtype，_detect_type 才能正确识别 binary({0,255})
        # / categorical；未覆盖区域填 src_nodata（缺省 0）。
        fill = 0 if src_nodata is None else int(src_nodata)
        out = np.where(acc == sentinel, fill, acc).astype(datasets[0].dtypes[0])
        nodata: Optional[float] = float(src_nodata) if src_nodata is not None else None
    else:
        out = np.where(acc == sentinel, np.nan, acc).astype(np.float64)
        nodata = None
    extent = (float(left), float(bottom), float(right), float(top))
    return out, out_h, out_w, dst_transform, nodata, extent


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze(
    job_id: str,
    filenames: List[str],
    mode: str = "continuous",
    custom_colors: Optional[Dict[str, str]] = None,
    unique_values: Optional[List[Any]] = None,
    cache_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Read one or more GeoTIFFs, classify them, render PNG, write info JSON.

    真正干活的是 :func:`_analyze`。这个外壳只负责一件事：**失败时释放掉
    缓存里的数据集句柄**。Windows 上一个还开着的文件句柄会挡住下一次
    ``os.replace`` —— 也就是「重新下载并发布同名结果」会直接失败。一个
    读不出数据的坏文件，不应该把自己的路径永久锁死。
    """
    try:
        return _analyze(
            job_id,
            filenames,
            mode=mode,
            custom_colors=custom_colors,
            unique_values=unique_values,
            cache_key=cache_key,
        )
    except Exception:
        _open_dataset.cache_clear()
        raise


def _analyze(
    job_id: str,
    filenames: List[str],
    mode: str = "continuous",
    custom_colors: Optional[Dict[str, str]] = None,
    unique_values: Optional[List[Any]] = None,
    cache_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Read one or more GeoTIFFs, classify them, render PNG, write info JSON.

    Args:
        filenames: 一个或多个 job_id 目录下的文件名。单文件时直接分析；多文件
            时先拼成一张再分析（自动处理不同 extent / CRS / 分辨率）。
        mode: ``"continuous"`` (default) — type-aware rendering that uses
            viridis for continuous rasters, black/white for binary, and a
            categorical palette for integer rasters with ≤32 unique values.
            ``"unique"`` — force render-by-unique-value regardless of dtype.
        custom_colors: 用户在前端临时指定的「值 → 颜色」映射，仅在
            ``mode="unique"`` 下生效；key 是字符串（JSON 反序列化结果）。
        unique_values: 用户手动指定要渲染的唯一值集合；仅在 ``mode="unique"``
            下生效。空列表 / None → 按数组实际 unique 值渲染；非空 → 只渲染
            这些值，其他像元按 nodata 处理。
        cache_key: PNG / info.json 的缓存键。默认：单文件用文件名本身，
            多文件用 ``_mosaic_cache_key(filenames)``（独立于各文件自己的
            单图预览缓存）。

    Returns:
        info dict，包含 ``mosaic`` 和 ``mosaic_count`` 字段标识是否为拼接图。
    """
    if not filenames:
        raise ValueError("filenames must not be empty")
    is_mosaic = len(filenames) > 1
    # 用第一张图作为路径 / bounds 的来源；多张时 PNG 也写到第一张图的路径上。
    primary_filename = filenames[0]
    # 合成图用独立缓存键：否则拼接会覆盖掉第一个文件自己的单图预览。
    cache_key = cache_key or (
        _mosaic_cache_key(filenames) if is_mosaic else primary_filename
    )
    # 分类元数据：单文件读自己的 sidecar；多文件把各分片的 sidecar 合并。
    class_metadata = (
        _mosaic_class_metadata(job_id, filenames)
        if is_mosaic
        else load_class_metadata(job_id, primary_filename)
    )

    if is_mosaic:
        # 多文件：拼成一张 ≤ MAX_PREVIEW_PX 的小图（内存安全，详见
        # _mosaic_rasters 的 docstring）。
        arr, out_h, out_w, mosaic_transform, nodata, extent = _mosaic_rasters(
            job_id, filenames
        )
        height, width = out_h, out_w
        # 合成图的地理范围取各源的真实 union bounds（目标 CRS = WGS84），
        # 而不是 ceil 出来的输出网格 —— 这样「分片拼回来」和「原图」的
        # bounds 完全一致。
        bounds = extent
        primary_ds = None
    else:
        primary_ds = _get_dataset(job_id, primary_filename)
        height, width = primary_ds.height, primary_ds.width
        out_h, out_w = _downsample_shape(height, width)
        arr = primary_ds.read(1, out_shape=(out_h, out_w), masked=False)
        nodata = primary_ds.nodata
        bounds = primary_ds.bounds

    png_path = _png_path(job_id, cache_key)
    info_path = _info_path(job_id, cache_key)
    _preview_dir(job_id).mkdir(parents=True, exist_ok=True)

    # mode="unique" 优先级最高：不论原始 dtype，全部按唯一值上色；
    # 唯一值过多 (>32) 时回退到连续模式，type 字段如实记为"连续"。
    if mode == "unique":
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
        arr_for_type = (
            primary_ds.read(1, out_shape=(min(height, 256), min(width, 256)), masked=False)
            if not is_mosaic
            else arr
        )
        # mosaic 模式下 arr 的 dtype 已还原成源的 dtype，直接探测即可。
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
            # class_metadata 已在函数开头算好（多文件时是各分片 sidecar 的合并）。
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

    # Normalize bounds to EPSG:4326 so the frontend can safely call
    # fromLonLat. rasterio's ds.bounds returns native CRS coordinates; yield
    # maps are often in EPSG:3857 or a Chinese projected CRS, which would put
    # the overlay canvas in the middle of the ocean if we passed them through
    # fromLonLat unchanged.
    # mosaic 模式下 bounds 已经是 WGS84（目标 CRS 固定），不用再转。
    if is_mosaic:
        left, bottom, right, top = (
            float(bounds[0]),
            float(bounds[1]),
            float(bounds[2]),
            float(bounds[3]),
        )
    else:
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
        # 源文件签名 —— 缓存命中时用来判断「文件是不是已经换过了」。
        "source_signature": _source_signature(job_id, filenames),
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
            if _cache_is_valid(cached, job_id, [filename]):
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
                cache_valid = _cache_is_valid(json.load(fh), job_id, [filename])
        except (json.JSONDecodeError, OSError):
            cache_valid = False
    if not cache_valid:
        analyze(job_id, [filename])
    return _png_path(job_id, filename)


# ---------------------------------------------------------------------------
# Mosaic (multi-file) public API
# ---------------------------------------------------------------------------

def get_mosaic_info(job_id: str, filenames: List[str]) -> Dict[str, Any]:
    """Return the info dict of the **mosaic** of several GeoTIFFs.

    与 :func:`get_info` 的区别：``get_info`` 看的是单个文件，这里看的是
    多个文件按 extent 拼成的那一张图。缓存键独立于各文件自己的单图预览，
    不会互相覆盖。
    """
    if not filenames:
        raise ValueError("filenames must not be empty")
    if len(filenames) == 1:
        return get_info(job_id, filenames[0])
    key = _mosaic_cache_key(filenames)
    info_path = _info_path(job_id, key)
    if info_path.is_file():
        try:
            with info_path.open("r", encoding="utf-8") as fh:
                cached = json.load(fh)
            if _cache_is_valid(cached, job_id, filenames):
                return cached
        except (json.JSONDecodeError, OSError):
            pass
    return analyze(job_id, filenames, cache_key=key)


def get_mosaic_preview_path(job_id: str, filenames: List[str]) -> Path:
    """Return the PNG path of the mosaic of several GeoTIFFs."""
    if not filenames:
        raise ValueError("filenames must not be empty")
    if len(filenames) == 1:
        return get_preview_path(job_id, filenames[0])
    key = _mosaic_cache_key(filenames)
    info_path = _info_path(job_id, key)
    cache_valid = False
    if info_path.is_file():
        try:
            with info_path.open("r", encoding="utf-8") as fh:
                cache_valid = _cache_is_valid(json.load(fh), job_id, filenames)
        except (json.JSONDecodeError, OSError):
            cache_valid = False
    if not cache_valid:
        analyze(job_id, filenames, cache_key=key)
    return _png_path(job_id, key)


def pixel_value(job_id: str, filename: str, lon: float, lat: float, band: int = 1) -> Dict[str, Any]:
    """Read the raw pixel value at a geographic coordinate."""
    ds = _get_dataset(job_id, filename)
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
    "get_mosaic_info",
    "get_mosaic_preview_path",
    "pixel_value",
    "clear_cache",
]