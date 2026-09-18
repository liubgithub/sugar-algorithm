# -*- coding: utf-8 -*-
"""运行结果处理：把算法产出的 tif 渲染为浏览器可显示的 png 预览图。"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

import numpy as np
import rasterio

# Windows 下中文字体
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False


def render_tif_preview(tif_path: Path, png_path: Path, title: str = "") -> Path:
    """读取单波段 tif，用 viridis 配色渲染 png 预览图（大影像自动降采样）。"""
    with rasterio.open(tif_path) as src:
        # 影像超过 1024 边长时降采样读取，避免大图预览占用过多内存
        scale = max(1, max(src.width, src.height) // 1024 + 1)
        arr = src.read(1, out_shape=(src.height // scale, src.width // scale))
        nodata = src.nodata

    data = arr.astype("float64")
    if nodata is not None:
        data[data == nodata] = np.nan
    data[~np.isfinite(data)] = np.nan

    vmin = np.nanmin(data) if np.any(np.isfinite(data)) else 0
    vmax = np.nanmax(data) if np.any(np.isfinite(data)) else 1
    if vmin == vmax:
        vmax = vmin + 1e-6

    fig, ax = plt.subplots(figsize=(6.4, 5.2), dpi=110)
    im = ax.imshow(data, cmap="viridis", vmin=vmin, vmax=vmax)
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    if title:
        ax.set_title(title)
    ax.set_axis_off()
    fig.tight_layout()
    fig.savefig(png_path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return png_path
