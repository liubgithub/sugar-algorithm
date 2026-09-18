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

from app.core.config import JOBS_DIR

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


def _stats_categorical(arr: np.ndarray, nodata: Optional[float]) -> Tuple[Dict[int, int], Dict[int, str]]:
    mask = (arr == nodata) if nodata is not None else np.zeros_like(arr, dtype=bool)
    valid = arr[~mask]
    unique, counts = np.unique(valid, return_counts=True)
    counts_dict: Dict[int, int] = {int(u): int(c) for u, c in zip(unique, counts)}
    # Discrete color per class.
    classes = {int(u): _pick_color(int(i), len(unique)) for i, u in enumerate(unique)}
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
# Public API
# ---------------------------------------------------------------------------

def analyze(job_id: str, filename: str) -> Dict[str, Any]:
    """Read the GeoTIFF, classify it, render PNG, write info JSON. Returns info dict."""
    ds = _open_dataset(job_id, filename)
    try:
        # Read band 1 (algorithms write single-band rasters).
        height, width = ds.height, ds.width
        out_h, out_w = _downsample_shape(height, width)
        arr = ds.read(1, out_shape=(out_h, out_w), masked=False)
    finally:
        # We keep ds in the LRU cache; do NOT close here.
        pass

    nodata = ds.nodata
    arr_for_type = ds.read(1, out_shape=(min(height, 256), min(width, 256)), masked=False)
    raster_type = _detect_type(arr_for_type)

    png_path = _png_path(job_id, filename)
    info_path = _info_path(job_id, filename)
    _preview_dir(job_id).mkdir(parents=True, exist_ok=True)

    if raster_type == "continuous":
        stats = _stats_continuous(arr, nodata)
        total_pixels = int(arr.size)
        nodata_pixels = int(stats.get("nodata_count") or 0)
        effective = max(total_pixels - nodata_pixels, 0)
        effective_ratio = (effective / total_pixels) if total_pixels else 0.0
        if total_pixels > 0 and effective_ratio < 0.05:
            # 全图基本是 nodata 时，viridis 渲染出来几乎是纯底色；用一张
            # 带说明的 PNG 替代，让用户立刻知道为什么空白。
            _render_nodata_warning(
                png_path,
                message=f"No effective pixels ({effective}/{total_pixels})",
            )
        else:
            _render_continuous(arr, stats, png_path, nodata)
        classes = None
    elif raster_type == "binary":
        stats = _stats_binary(arr, nodata)
        total_pixels = int(arr.size)
        nodata_pixels = int(stats.get("nodata_count") or 0)
        effective = max(total_pixels - nodata_pixels, 0)
        effective_ratio = (effective / total_pixels) if total_pixels else 0.0
        if total_pixels > 0 and effective_ratio < 0.05:
            _render_nodata_warning(png_path)
        else:
            _render_binary(arr, png_path, nodata)
        # Provide a trivial legend (foreground vs background).
        classes = {0: "#ffffff", 1: "#000000"}
        if stats["count_0"] == 0 and stats["count_1"] > 0:
            classes = {0: "#ffffff", 255: "#000000"}
    else:
        counts, classes = _stats_categorical(arr, nodata)
        total_pixels = int(arr.size)
        nodata_pixels = int((arr == nodata).sum()) if nodata is not None else 0
        effective = max(total_pixels - nodata_pixels, 0)
        effective_ratio = (effective / total_pixels) if total_pixels else 0.0
        if total_pixels > 0 and effective_ratio < 0.05:
            _render_nodata_warning(png_path)
        else:
            _render_categorical(arr, classes, png_path, nodata)
        stats = {"class_counts": counts, "nodata_count": nodata_pixels}

    bounds = ds.bounds  # rasterio.coords.BoundingBox
    info_payload: Dict[str, Any] = {
        "job_id": job_id,
        "filename": filename,
        "type": raster_type,
        "width": int(width),
        "height": int(height),
        "preview_width": int(out_w),
        "preview_height": int(out_h),
        "bounds": [
            [float(bounds.left), float(bounds.bottom)],
            [float(bounds.right), float(bounds.top)],
        ],
        "stats": stats,
        "classes": classes,
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
    """Return the cached info dict, generating it on first request."""
    info_path = _info_path(job_id, filename)
    if info_path.is_file():
        try:
            with info_path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            pass
    return analyze(job_id, filename)


def get_preview_path(job_id: str, filename: str) -> Path:
    """Return PNG path, generating it on first request."""
    png_path = _png_path(job_id, filename)
    if not png_path.is_file():
        analyze(job_id, filename)
    return png_path


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