# -*- coding: utf-8 -*-
"""API 数据模型。"""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class AlgorithmParam(BaseModel):
    name: str
    label: str
    # csv/tif/xlsx/json = 从数据文件夹选择文件；month/text/number = 直接传标量值
    type: Literal["csv", "tif", "xlsx", "json", "month", "text", "number"]
    required: bool = True


class AlgorithmInfo(BaseModel):
    id: str
    name: str
    description: str
    params: List[AlgorithmParam]


class DataFileInfo(BaseModel):
    name: str
    type: Literal["csv", "tif", "xlsx", "json"]
    size_mb: float


class RunRequest(BaseModel):
    algorithm_id: str
    inputs: dict[str, str] = Field(default_factory=dict)


class MetricItem(BaseModel):
    name: str
    value: float
    unit: str = ""
    decimals: int = 3


class TableData(BaseModel):
    name: str
    columns: List[str]
    rows: List[list]
    csv_url: Optional[str] = None


class RasterResult(BaseModel):
    name: str
    png_url: str
    tif_url: str


class RunResult(BaseModel):
    run_id: str
    algorithm_id: str
    algorithm_name: str
    message: str
    metrics: List[MetricItem]
    tables: List[TableData]
    rasters: List[RasterResult]
