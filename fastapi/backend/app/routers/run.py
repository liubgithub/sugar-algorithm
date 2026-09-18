# -*- coding: utf-8 -*-
"""算法运行与结果下载接口。"""
import re
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..config import DATA_DIR, OUTPUTS_DIR
from ..schemas import RunRequest, RunResult
from ..services import registry
from ..services.result_builder import render_tif_preview

router = APIRouter(tags=["run"])

_RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_NUMBER_PATTERN = re.compile(r"^-?\d+(\.\d+)?$")


def _resolve_data_file(filename: str) -> Path:
    """校验文件名为 DATA_DIR 内的相对路径（支持子目录，拒绝 .. 与绝对路径），返回绝对路径。"""
    if not filename or Path(filename).is_absolute() or ".." in Path(filename).parts:
        raise HTTPException(status_code=400, detail=f"非法文件名：{filename}")
    path = (DATA_DIR / filename).resolve()
    if DATA_DIR.resolve() not in path.parents or not path.is_file():
        raise HTTPException(status_code=400, detail=f"数据文件不存在：{filename}")
    return path


def _check_file_type(path: Path, expected: str, param_label: str) -> None:
    suffix = path.suffix.lower().lstrip(".")
    actual = "tif" if suffix in ("tif", "tiff") else (suffix if suffix in ("csv", "xlsx", "json", "zip") else None)
    if expected == "shp" and actual == "zip":
        actual = "shp"  # shp 矢量边界以 zip 压缩包上传（内含 .shp/.shx/.dbf/.prj）
    if actual != expected:
        raise HTTPException(
            status_code=400,
            detail=f"参数「{param_label}」需要 {expected} 文件，但选择的是 {actual or '不支持的类型'} 文件",
        )


@router.post("/run", response_model=RunResult, summary="运行算法")
def run_algorithm(request: RunRequest):
    module = registry.load_algorithm(request.algorithm_id)
    if module is None:
        raise HTTPException(status_code=404, detail="算法不存在")

    meta = module.ALGO_META

    # 1. 参数校验：必填项齐全、文件存在、类型匹配
    inputs = {}
    for param in meta["params"]:
        value = request.inputs.get(param["name"]) or ""
        if not value:
            if param.get("required", True):
                raise HTTPException(status_code=400, detail=f"缺少必填参数：{param['label']}")
            continue
        if param["type"] == "month":
            # 标量参数：校验 YYYY-MM 格式后原样传给算法
            if not _MONTH_PATTERN.fullmatch(value):
                raise HTTPException(status_code=400, detail=f"参数「{param['label']}」格式须为 YYYY-MM，收到：{value}")
            inputs[param["name"]] = value
            continue
        if param["type"] in ("text", "number"):
            # 标量参数：text 原样传递；number 校验为数字后转 float 传给算法
            value = str(value).strip()
            if param["type"] == "number":
                if not _NUMBER_PATTERN.fullmatch(value):
                    raise HTTPException(status_code=400, detail=f"参数「{param['label']}」须为数字，收到：{value}")
                inputs[param["name"]] = float(value)
            else:
                inputs[param["name"]] = value
            continue
        path = _resolve_data_file(value)
        _check_file_type(path, param["type"], param["label"])
        inputs[param["name"]] = path

    # 2. 执行算法（同步）
    run_id = uuid4().hex
    workdir = OUTPUTS_DIR / run_id
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        raw = module.run(inputs, workdir)
    except HTTPException:
        raise
    except Exception as exc:
        # 把完整错误信息打印到服务器控制台（前端只能看到摘要），方便在终端直接定位原因
        import traceback

        print(f"\n[run] 算法「{meta['id']}」执行失败，完整堆栈：")
        traceback.print_exc()
        print()
        detail = str(exc) or f"{type(exc).__name__}（无错误消息，详见服务器控制台）"
        raise HTTPException(status_code=400, detail=f"算法执行失败：{detail}")

    # 3. 构建响应：tif 渲染 png 预览，csv 提供下载链接
    rasters = []
    for item in raw.get("rasters", []):
        tif_path = Path(item["tif"])
        png_path = Path(item.get("png", tif_path.with_suffix(".png")))
        if not png_path.is_file():
            png_path = render_tif_preview(tif_path, png_path, item["name"])
        rasters.append({
            "name": item["name"],
            "png_url": f"/api/outputs/{run_id}/{png_path.name}",
            "tif_url": f"/api/outputs/{run_id}/{tif_path.name}",
        })

    tables = []
    for item in raw.get("tables", []):
        csv_url = None
        if item.get("file") is not None and Path(item["file"]).is_file():
            csv_url = f"/api/outputs/{run_id}/{Path(item['file']).name}"
        tables.append({
            "name": item["name"],
            "columns": item["columns"],
            "rows": item["rows"],
            "csv_url": csv_url,
        })

    return RunResult(
        run_id=run_id,
        algorithm_id=meta["id"],
        algorithm_name=meta["name"],
        message=raw.get("message", "运行成功"),
        metrics=raw.get("metrics", []),
        tables=tables,
        rasters=rasters,
    )


@router.get("/outputs/{run_id}/{filename}", summary="下载运行结果文件")
def download_output(run_id: str, filename: str):
    # run_id 必须是 32 位十六进制，filename 不得含路径分隔符（双重防路径穿越）
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise HTTPException(status_code=404, detail="无效的运行编号")
    if not filename or Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="无效的文件名")
    path = (OUTPUTS_DIR / run_id / filename).resolve()
    if OUTPUTS_DIR.resolve() not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(path, filename=filename)
