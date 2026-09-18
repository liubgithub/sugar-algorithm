# -*- coding: utf-8 -*-
"""算法列表、数据文件列表与数据文件上传接口。"""
import shutil
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ..config import ALLOWED_TYPES, DATA_DIR
from ..services import registry

router = APIRouter(tags=["algorithms"])


@router.get("/algorithms", summary="列出可用算法")
def list_algorithms():
    return registry.list_algorithms()


@router.get("/data", summary="列出数据文件夹中的可用文件")
def list_data_files():
    """列出数据文件夹中的可用文件（支持子目录，name 为相对路径）。"""
    files = []
    for path in sorted(DATA_DIR.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower().lstrip(".")
        if suffix not in ALLOWED_TYPES:
            continue
        # tiff 归并为 tif 类型
        ftype = "tif" if suffix == "tiff" else suffix
        files.append({
            # 统一正斜杠，避免 Windows 反斜杠出现在前端与请求参数里
            "name": str(path.relative_to(DATA_DIR)).replace("\\", "/"),
            "type": ftype,
            "size_mb": round(path.stat().st_size / 1024 / 1024, 2),
        })
    return files


@router.post("/upload", summary="上传数据文件")
def upload_data_file(file: UploadFile = File(...), folder: str = Form("")):
    """把前端上传的文件保存到数据文件夹（可指定子目录），返回相对路径供运行参数使用。

    文件保存在服务器端 backend/data/ 下，前端拿到返回的 name 后与普通数据文件
    一样作为 run 请求的参数值提交；算法真正使用服务器本地路径，不经过浏览器中转。
    """
    filename = Path(file.filename or "").name
    if not filename:
        raise HTTPException(status_code=400, detail="文件名无效")

    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    suffix = "tif" if suffix == "tiff" else suffix
    if suffix not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型：{suffix or '无后缀'}（允许 {', '.join(ALLOWED_TYPES)}）")

    # 目标目录必须是 DATA_DIR 或其子目录：拒绝绝对路径与 .. 穿越
    folder = (folder or "").strip().strip("/\\")
    try:
        dest_dir = (DATA_DIR / folder).resolve() if folder else DATA_DIR.resolve()
    except (ValueError, OSError):
        raise HTTPException(status_code=400, detail=f"非法子目录：{folder}")
    if dest_dir != DATA_DIR.resolve() and DATA_DIR.resolve() not in dest_dir.parents:
        raise HTTPException(status_code=400, detail=f"非法子目录：{folder}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    try:
        with dest.open("wb") as out:
            shutil.copyfileobj(file.file, out)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"文件保存失败：{exc}")

    rel = str(dest.relative_to(DATA_DIR)).replace("\\", "/")
    return {"name": rel, "type": suffix, "size_mb": round(dest.stat().st_size / 1024 / 1024, 2)}
