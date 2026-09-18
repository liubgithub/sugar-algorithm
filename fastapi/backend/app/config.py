# -*- coding: utf-8 -*-
"""全局路径常量（配置集中管理）"""
from pathlib import Path

# backend/ 目录（本文件在 backend/app/config.py）
BASE_DIR = Path(__file__).resolve().parents[1]

# 算法文件夹：每个算法一个 .py 文件，按 ALGO_META/run 规范编写
ALGORITHMS_DIR = BASE_DIR / "algorithms"

# 数据文件夹：算法参数文件统一放在这里，前端可选择已有文件，也可通过 /api/upload 上传
DATA_DIR = BASE_DIR / "data"

# 每次运行的结果目录 outputs/{run_id}/
OUTPUTS_DIR = BASE_DIR / "outputs"

# 允许的数据文件类型（json 用于 GEE 服务账号凭证等算法参数文件；
# zip 用于 shp 矢量边界的压缩包上传）
ALLOWED_TYPES = ("csv", "tif", "tiff", "xlsx", "json", "zip")
