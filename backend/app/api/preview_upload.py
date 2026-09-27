"""Upload-an-arbitrary-TIF-and-return-a-preview endpoint.

Used by the frontend `Onlyshow` page. The browser cannot decode very large
GeoTIFFs in-process (a single huge strip requires a Float32Array whose size
exceeds Chrome's per-allocation cap, e.g. 10000x10000 float ≈ 400MB), so we
hand the bytes to rasterio on the server and ship back the downsampled PNG
plus statistics.

This wraps `preview_service.analyze()` — the same pipeline that produces
previews for completed job rasters — so the visual treatment (viridis for
continuous, categorical palettes for classified, etc.) matches the rest of
the platform.
"""
from __future__ import annotations

import base64
import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import List

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.core.config import JOBS_DIR
from app.services import preview_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/preview", tags=["preview"])


def _purge_stale_preview_jobs() -> None:
    """启动时清理 JOBS_DIR 下残留的 _preview_<uuid>/ 临时目录。

    每个 /api/preview/tif 请求都会创建 _preview_<12hex>/ 临时目录，正常在
    finally 里 rmtree 清掉。但进程被 kill / OOM / 异常断电时可能留下孤儿，
    长期反复上传会让 data/jobs/ 越攒越大。这里在模块加载时扫一次兜底清理，
    只匹配 _preview_ 前缀的目录，绝不触碰正常的算法任务目录（32 位 hex）。

    整个扫描+删除是同步阻塞调用，但 JOBS_DIR 同级条目通常 < 1000，开销可
    忽略；如果以后目录规模爆炸，可换成惰性清理（在请求里顺带清一个）或迁移到
    lifespan startup 里异步执行。
    """
    jobs_root = Path(JOBS_DIR)
    if not jobs_root.is_dir():
        return
    removed = 0
    for entry in jobs_root.iterdir():
        if not entry.is_dir():
            continue
        if not entry.name.startswith("_preview_"):
            continue
        try:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
            logger.info("purged stale preview job dir: %s", entry.name)
        except OSError as exc:  # pragma: no cover — 防御性日志
            logger.warning("failed to purge %s: %s", entry.name, exc)
    if removed:
        logger.info("startup cleanup: removed %d stale _preview_* dir(s)", removed)


# 模块加载即触发一次（main.py 在 include router 前会 import 该模块）。
# 比 lifespan startup 更早，但成本很低；后续请求照常被 finally 清理。
_purge_stale_preview_jobs()


def _safe_name(name: str) -> str:
    """Strip path components; collapse non-alnum to '_'. Always returns .tif suffix."""
    base = Path(name).name
    cleaned = "".join(c if c.isalnum() or c in "._-" else "_" for c in base)
    if not cleaned:
        cleaned = "upload.tif"
    if not cleaned.lower().endswith((".tif", ".tiff")):
        cleaned = cleaned + ".tif"
    return cleaned


@router.post("/tif")
async def preview_uploaded_tif(
    files: List[UploadFile] = File(..., description="一个或多个 .tif/.tiff；多文件时按 rasterio.merge 拼接"),
    mode: str = Form("continuous"),
    custom_colors: str = Form("", description='JSON 字符串，如 \'{"10":"#ff0000"}\'；仅 mode=unique 生效'),
    unique_values: str = Form("", description='JSON 数组字符串，如 \'[10, 20, 30, 40, 50]\'；留空表示按实际 unique 值渲染'),
) -> dict:
    """接受一个或多个 .tif/.tiff，rasterio 读取并重采样到 MAX_PREVIEW_PX（1024）以内，
    返回 PNG（base64）+ 像元统计 + 类型信息。

    多文件场景：用 rasterio.merge 按 extent 自动拼接，尺寸 / CRS / 分辨率不一致时
    会自动重采样对齐。分类栅格会自动用 mode 重采样以保留整数类目值（10/20/30 不会被
    插值成 13/14/15 这种伪连续值）。拼好的图与单文件共用同一渲染管线（连续值 /
    唯一值 / 二值 / 分类）。

    临时文件写到 data/jobs/_preview_<uuid>/，处理完立刻清理 —— 不污染任何
    算法任务目录。

    Form fields:
      - mode: ``"continuous"``（默认）或 ``"unique"``。
        ``"unique"`` 强制按唯一值上色，适用于把连续栅格按"不同值不同颜色"
        查看；唯一值超过 32 个时自动回退到连续模式。
      - custom_colors: ``mode=unique`` 下的可选颜色覆盖；JSON 对象，key 是
        字符串（值的整数表示，如 ``"10"``），value 是 ``#rrggbb``。
      - unique_values: 用户手动指定要渲染的唯一值集合，JSON 数组，如
        ``"[10, 20, 30, 40, 50]"``。仅 ``mode=unique`` 生效；集合外的像元
        按 nodata 处理。留空 → 按栅格实际 unique 值渲染。
    """
    if not files:
        raise HTTPException(status_code=400, detail="missing file(s)")
    # mode 容错：未知值一律按连续处理，避免拼写错误时直接报错。
    normalized_mode = mode if mode in ("continuous", "unique") else "continuous"
    # custom_colors 容错：解析失败时忽略，仍按默认调色板。
    parsed_custom_colors: dict = {}
    if custom_colors and custom_colors.strip():
        try:
            obj = json.loads(custom_colors)
            if isinstance(obj, dict):
                # 验证格式：{str: "#rrggbb"}
                for k, v in obj.items():
                    if isinstance(k, str) and isinstance(v, str) and v.startswith("#") and len(v) >= 4:
                        parsed_custom_colors[k] = v
        except (ValueError, TypeError):
            pass
    # unique_values 容错：解析失败 / 非数组 / 空数组都当成"未指定"，保留原行为。
    parsed_unique_values: list = []
    if unique_values and unique_values.strip():
        try:
            obj = json.loads(unique_values)
            if isinstance(obj, list) and obj:
                # 全部尝试转成数值；失败跳过。
                for v in obj:
                    try:
                        parsed_unique_values.append(int(v))
                    except (ValueError, TypeError):
                        try:
                            parsed_unique_values.append(float(v))
                        except (ValueError, TypeError):
                            continue
        except (ValueError, TypeError):
            pass

    # 临时 job_id：preview_service 强依赖 (job_id, filename) 来定位 TIF 与写出 PNG，
    # 复用它的好处是颜色映射、stats、cmap 等所有逻辑都跟任务结果一致。
    job_id = f"_preview_{uuid.uuid4().hex[:12]}"
    job_dir = Path(JOBS_DIR) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # 写盘所有文件 + 收集 safe 名。
    safe_names: List[str] = []
    try:
        for f in files:
            if not f.filename:
                raise HTTPException(status_code=400, detail="missing filename")
            safe = _safe_name(f.filename)
            if not safe.lower().endswith((".tif", ".tiff")):
                raise HTTPException(status_code=400, detail="only .tif/.tiff is supported")
            saved_path = job_dir / safe
            with saved_path.open("wb") as out:
                while True:
                    chunk = await f.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            safe_names.append(safe)

        try:
            info = preview_service.analyze(
                job_id,
                safe_names,
                mode=normalized_mode,
                custom_colors=parsed_custom_colors,
                unique_values=parsed_unique_values or None,
            )
        except Exception as exc:  # noqa: BLE001
            # rasterio 在打不开 / 不是 GeoTIFF / 无波段时报各种错；统一兜底成 400。
            raise HTTPException(status_code=400, detail=f"TIF 解析失败：{exc}") from exc

        png_path = job_dir / "preview" / f"{safe_names[0]}.png"
        if not png_path.is_file():
            raise HTTPException(status_code=500, detail="preview not generated")
        with png_path.open("rb") as fh:
            png_b64 = base64.b64encode(fh.read()).decode("ascii")

        return {
            "filename": safe_names[0],
            "filenames": safe_names,
            "mosaic": bool(info.get("mosaic")),
            "mosaic_count": int(info.get("mosaic_count") or len(safe_names)),
            "width": info.get("width"),
            "height": info.get("height"),
            "preview_width": info.get("preview_width"),
            "preview_height": info.get("preview_height"),
            "type": info.get("type"),
            "stats": info.get("stats") or {},
            "cmap": info.get("cmap"),
            "nodata": info.get("nodata"),
            "effective_pixel_ratio": info.get("effective_pixel_ratio"),
            "effective_pixel_count": info.get("effective_pixel_count"),
            "total_pixel_count": info.get("total_pixel_count"),
            "classes": info.get("classes") or {},
            "class_metadata": info.get("class_metadata") or {},
            "mode": normalized_mode,
            "unique_values": parsed_unique_values,
            "preview_png_base64": png_b64,
        }
    finally:
        # 无论成功失败都清掉临时目录，避免反复上传后 data/jobs/ 越攒越大。
        shutil.rmtree(job_dir, ignore_errors=True)