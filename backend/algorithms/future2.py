# -*- coding: utf-8 -*-
from __future__ import annotations

"""
GEE 全时序遥感特征提取算法（future2 版）——波段命名 {波段}_{月份}，严格对齐老版本数据。

本文件 = 原生 future2.py 算法核心（原样保留）+ 平台接入层（ALGO_META / run()）。

对原生脚本的改动只有四处（都在算法核心之外）：
  1. 删除模块顶部的 ee.Authenticate()/ee.Initialize()：平台注册表每次列算法都会
     执行模块代码，模块级鉴权会打开浏览器/阻塞接口；初始化移入 run() 内按需执行
     （支持本机已登录凭据与服务账号 JSON 两种方式）。
  2. 写死的样本点资产 ID 改为平台参数：上传样本点 CSV（客户端构造
     FeatureCollection，免上传 GEE 资产）或填写已有表资产 ID。
  3. 删除 Drive 导出：平台同步返回结果，改用分块 sampleRegions + getInfo
     取回特征，写成 CSV 由前端展示与下载。
  4. 特征提取改为「块级 AOI 构图 + 空间分块 + 并行」：交互式 getInfo 有 8GB
     用户内存限制，用全样本 AOI 构的图采样时 GEE 按全域求值必超限（实测
     使只采 250 点也报 User memory limit exceeded）；每块用自己的小 AOI
     构图后每次求值只算局部瓦片（实测 0.75° 范围 2000 点 133 波段稳定通过）。
     原版走 Drive 批量导出不受此限，因此「原版能用、直接改 getInfo 会出错」。

脚本结构：
1. 全局配置：默认 GEE 项目、归一化极值字典（minMaxDict）、分块参数。
2. GEE 初始化：本机凭据 / 服务账号 JSON，瞬时网络错误自动重试。
3. 样本点加载：CSV 坐标列名自动识别 → ee.Feature 列表（或直接用资产 ID）。
4. 核心算法（原生脚本原样）：月度 Sentinel-2/1 合成、指数、归一化、
   年际中值填补、toBands 压平 + 去前缀 → 12 个月 132 波段 + DEM = 133 波段。
5. 特征提取：块级 AOI 构图 + 客户端小块 FC + 多线程并行 getInfo。
6. 结果组装：特征 CSV、波段有效性统计、全年 NDVI 预览 tif、指标。
7. 平台接入：ALGO_META 参数声明 + run() 入口。
"""

import json
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import numpy as np
import pandas as pd

import ee

# 把本文件所在目录加入 sys.path，保证被平台 registry（任意工作目录）加载时
# 仍能 import 同目录辅助模块。
_ALGO_DIR = Path(__file__).resolve().parent
if str(_ALGO_DIR) not in sys.path:
    sys.path.insert(0, str(_ALGO_DIR))

# =============================================================================
# 1. 全局配置
# =============================================================================
# GEE Cloud 项目 ID：默认值来自原生脚本，部署到其他环境时可通过平台参数覆盖。
DEFAULT_PROJECT = "test1-506208"
DEFAULT_YEAR = 2025
DEFAULT_CLOUD_PCT = 80.0
DEFAULT_SCALE = 10.0

# 采样分块大小（点数上限）：一次 getInfo 只取这么多点的 133 波段结果，
# 15000 点全量一次性返回约 40MB，超过 GEE 返回值上限。
CHUNK_SIZE = 2000

# 块的空间跨度上限（度）：每块影像用块级 AOI 构图（见 extract_chunk），
# 跨度决定构图影像集范围与求值区域；实测 0.75° 内可通过，取 0.5° 留余量。
SPATIAL_CHUNK_MAX_DEG = 0.5

# 分块网格大小（度）：分块时先把样本点落到网格单元，再合并相邻单元成块。
SPATIAL_CELL_DEG = 0.25

# sampleRegions 的 tileScale（瓦片 256 × tileScale 像素）。原生脚本用 16，
# 但那是走 Drive 批量导出（内存限制宽松）；交互式 getInfo 有 8GB 用户内存
# 限制，块级构图 + tileScale=4 是实测通过的配置（W2/W3 实验），取 4。
TILE_SCALE = 4

# 并行提取线程数：每块一次独立 getInfo，并发执行把总时长压到几分钟内。
MAX_WORKERS = 8

# Web 端表格最多展示的行数（完整数据通过 csv 文件下载）。
WEB_PREVIEW_ROWS = 300

# 归一化极值字典 (统一缩放至 0~1)，与原生脚本一致。
minMaxDict = {
    'B2': [0, 0.4], 'B3': [0, 0.4], 'B4': [0, 0.4],
    'B8': [0, 0.6], 'B11': [0, 0.5], 'B12': [0, 0.4],
    'NDVI': [-0.2, 0.9], 'EVI': [0, 1], 'NDWI': [-0.5, 0.5],
    'VV': [-25, -5], 'VH': [-30, -10]
}

# 波段顺序即输出特征列顺序（11 个/月 × 12 月 + 1 个地形 = 133），
# 列名格式为 {波段}_{月份}（如 VH_1、B2_12），与原生脚本一致。
MONTHLY_BANDS = ['B2', 'B3', 'B4', 'B8', 'B11', 'B12', 'NDVI', 'EVI', 'NDWI', 'VV', 'VH']
DEM_BAND = 'elevation'

# 月度合成依赖的原始波段名（S2 光谱 6 个 + S1 后向散射 2 个）
S2_SOURCE_BANDS = ['B2', 'B3', 'B4', 'B8', 'B11', 'B12']
S1_SOURCE_BANDS = ['VV', 'VH']

# 特征列识别正则：{波段}_{1..12}（用于波段有效性统计）
BAND_COL_PATTERN = re.compile(r"^(B2|B3|B4|B8|B11|B12|NDVI|EVI|NDWI|VV|VH)_([1-9]|1[0-2])$")


# =============================================================================
# 2. GEE 初始化
# =============================================================================
def init_gee(project: str, credentials_file: Optional[Path]) -> None:
    """初始化 GEE 连接（替代原生脚本模块顶部的 ee.Authenticate + ee.Initialize）。

    优先复用当前进程已初始化的凭据；否则：
      - 提供了服务账号 JSON 文件 → ee.ServiceAccountCredentials（部署服务器时推荐）；
      - 未提供 → 使用本机 `earthengine authenticate` 已保存的用户凭据。
    Google OAuth 端点偶尔出现瞬时网络错误（SSL EOF 等），重试 3 次后再放弃；
    失败时抛出带操作指引的中文错误，由平台转成错误消息展示给前端。
    """
    if getattr(ee.data, "_credentials", None) is not None:
        return
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            if credentials_file is not None:
                with open(credentials_file, "r", encoding="utf-8") as f:
                    info = json.load(f)
                credentials = ee.ServiceAccountCredentials(info["client_email"], str(credentials_file))
                ee.Initialize(credentials=credentials, project=project)
            else:
                ee.Initialize(project=project)
            return
        except Exception as exc:
            last_exc = exc
            print(f"  [重试 {attempt + 1}/3] GEE 初始化网络错误：{exc}")
            time.sleep(5 * (attempt + 1))
    raise ValueError(
        "GEE 初始化失败。请检查："
        "1) 本机是否已执行过 `earthengine authenticate`（且登录账号拥有所选 GEE 项目权限）；"
        "2) 若提示缺少项目权限，请用错误信息中的 GCP 控制台链接为该账号授予 "
        "Service Usage Consumer 角色，或换用拥有项目的账号重新 authenticate；"
        "3) 部署到服务器时上传 GEE 服务账号 JSON 凭证。原始错误：" + str(last_exc)
    ) from last_exc


# =============================================================================
# 3. 样本点加载：CSV → 客户端 FeatureCollection（免上传 GEE 资产）
# =============================================================================
# 经纬度列名自动识别（英文大小写不敏感 + 中文），无需用户改表头。
LON_CANDIDATES = ["longitude", "lon", "lng", "long", "x", "经度"]
LAT_CANDIDATES = ["latitude", "lat", "y", "纬度"]


def _find_column(columns: List[str], candidates: List[str]) -> Optional[str]:
    for cand in candidates:
        for col in columns:
            if col.strip().lower() == cand or col.strip() == cand:
                return col
    return None


def parse_sample_csv(csv_path: Path, label_property: str) -> Tuple[pd.DataFrame, str, str, int]:
    """纯 pandas 解析样本点 CSV（不依赖 GEE，便于本地测试）。

    自动识别经度/纬度列，剔除坐标缺失或越界的行，保留原始行号索引
    （后续作为 point_id 用于结果合并）。CSV 编码默认 utf-8，失败时退回 gbk。
    """
    try:
        df = pd.read_csv(csv_path)
    except UnicodeDecodeError:
        df = pd.read_csv(csv_path, encoding="gbk")
    df.columns = [str(c).strip() for c in df.columns]

    lon_col = _find_column(df.columns, LON_CANDIDATES)
    lat_col = _find_column(df.columns, LAT_CANDIDATES)
    if lon_col is None or lat_col is None:
        raise ValueError(
            f"样本点 CSV 未识别出经度/纬度列（可用列：{list(df.columns)}）。"
            "支持：Longitude/Lon/LNG/X/经度 与 Latitude/Lat/Y/纬度。"
        )
    if label_property not in df.columns:
        raise ValueError(
            f"样本点 CSV 缺少标签属性列「{label_property}」（可用列：{list(df.columns)}）。"
            "请修改标签属性列名参数，或调整 CSV 表头。"
        )

    df[lon_col] = pd.to_numeric(df[lon_col], errors="coerce")
    df[lat_col] = pd.to_numeric(df[lat_col], errors="coerce")
    valid = (
        df[lon_col].notna() & df[lat_col].notna()
        & df[lon_col].between(-180, 180) & df[lat_col].between(-90, 90)
    )
    dropped = int((~valid).sum())
    df = df[valid].copy()
    if df.empty:
        raise ValueError("样本点 CSV 中没有坐标有效的行。")
    # point_id 用原始行号，供 GEE 提取结果按行合并回原始表
    df["point_id"] = df.index
    return df, lon_col, lat_col, dropped


def features_from_dataframe(df: pd.DataFrame, lon_col: str, lat_col: str) -> List[ee.Feature]:
    """把解析好的样本表构造成 ee.Feature 列表（客户端构造，随计算请求发给 GEE）。

    原始 CSV 的所有列（除坐标列外）都作为属性保留；每点加 point_id
    用于把 GEE 提取结果按行合并回原始表。注意：构造 Geometry 前必须先初始化 GEE。
    """
    features = []
    for idx, row in df.iterrows():
        props = {}
        for col, val in row.items():
            if col in (lon_col, lat_col, "point_id"):
                continue
            # GEE 客户端序列化不接受 numpy 标量/NaN，这里统一转原生类型
            if isinstance(val, np.floating):
                val = None if not np.isfinite(val) else float(val)
            elif isinstance(val, np.integer):
                val = int(val)
            elif isinstance(val, np.bool_):
                val = bool(val)
            elif isinstance(val, float) and not np.isfinite(val):
                val = None
            if val is not None:
                props[col] = val
        props["point_id"] = idx
        features.append(ee.Feature(ee.Geometry.Point([float(row[lon_col]), float(row[lat_col])]), props))
    return features


def add_point_ids(fc, n: int):
    """给服务端 FeatureCollection 补 point_id（仅资产 ID 模式需要，CSV 模式客户端已带）。"""
    index_list = ee.List.sequence(0, ee.Number(n).subtract(1))
    return ee.FeatureCollection(
        index_list.map(lambda i: ee.Feature(fc.toList(n).get(i)).set("point_id", i))
    )


def build_spatial_chunks(df: pd.DataFrame, lon_col: str, lat_col: str) -> List[List[int]]:
    """按网格单元把样本点聚成空间连续、面积受控的分块，返回每块的点 id 列表。

    每块影像用块级 AOI 构图（见 extract_chunk），因此块的跨度必须受控：
      - 点数 ≤ CHUNK_SIZE（GEE 返回值上限）；
      - 经纬度跨度 ≤ SPATIAL_CHUNK_MAX_DEG（块级构图下 0.75° 实测可通过，
        0.5° 留余量；超限说明块内影像集/求值区域过大，有内存风险）。

    做法：每个点落到 SPATIAL_CELL_DEG 网格单元 → 单元号按「纬度带优先、
    经度其次」排序（相邻单元号在空间上相邻）→ 顺序合并相邻单元，
    合并后点数或跨度超限即开新块。
    """
    cell_lon = (df[lon_col] // SPATIAL_CELL_DEG).astype(int)
    cell_lat = (df[lat_col] // SPATIAL_CELL_DEG).astype(int)
    # 二维网格映射为一维编号：纬度带 × 10000 + 经度单元（经度单元数远小于 10000）
    df = df.assign(_cell=(cell_lat * 10000 + cell_lon))
    groups = df.groupby("_cell", sort=True)

    # 每个单元的点 id 与坐标范围
    cells = []
    for cell_id, idxs in groups.groups.items():
        sub = df.loc[idxs]
        cells.append({
            "ids": [int(i) for i in sub["point_id"]],
            "min_lon": float(sub[lon_col].min()),
            "max_lon": float(sub[lon_col].max()),
            "min_lat": float(sub[lat_col].min()),
            "max_lat": float(sub[lat_col].max()),
        })

    chunks: List[List[int]] = []
    cur_ids: List[int] = []
    cur_min_lon = cur_max_lon = cur_min_lat = cur_max_lat = 0.0
    for info in cells:
        if not cur_ids:
            cur_ids, cur_min_lon, cur_max_lon = info["ids"], info["min_lon"], info["max_lon"]
            cur_min_lat, cur_max_lat = info["min_lat"], info["max_lat"]
            continue
        n_ok = len(cur_ids) + len(info["ids"]) <= CHUNK_SIZE
        span_ok = (
            max(cur_max_lon, info["max_lon"]) - min(cur_min_lon, info["min_lon"]) <= SPATIAL_CHUNK_MAX_DEG
            and max(cur_max_lat, info["max_lat"]) - min(cur_min_lat, info["min_lat"]) <= SPATIAL_CHUNK_MAX_DEG
        )
        if n_ok and span_ok:
            cur_ids += info["ids"]
            cur_min_lon = min(cur_min_lon, info["min_lon"])
            cur_max_lon = max(cur_max_lon, info["max_lon"])
            cur_min_lat = min(cur_min_lat, info["min_lat"])
            cur_max_lat = max(cur_max_lat, info["max_lat"])
        else:
            chunks.append(cur_ids)
            cur_ids, cur_min_lon, cur_max_lon = info["ids"], info["min_lon"], info["max_lon"]
            cur_min_lat, cur_max_lat = info["min_lat"], info["max_lat"]
    if cur_ids:
        chunks.append(cur_ids)
    return chunks


# =============================================================================
# 4. 核心算法（原生脚本原样，只把样本点/云量阈值参数化）
# =============================================================================
def maskS2clouds(image):
    """Sentinel-2 云掩膜：QA60 bit10(密集云)/bit11(卷云) 置掩，并缩放到地表反射率 0~1。"""
    qa = image.select('QA60')
    mask = qa.bitwiseAnd(1 << 10).eq(0).And(qa.bitwiseAnd(1 << 11).eq(0))
    return image.updateMask(mask).divide(10000)


def normalizeBand(img, bandName, min_val, max_val):
    """单波段线性归一化到 0~1 并 clamp。"""
    val = img.select(bandName)
    norm = val.subtract(min_val).divide(ee.Number(max_val).subtract(min_val))
    return norm.clamp(0, 1).rename(bandName)


def empty_named_image(band_names: List[str]):
    """构造全掩膜的命名空波段影像（数据缺失月份的占位）。

    某月无可用影像时（如该区域当月无 Sentinel-1 数据），影像集 median()
    返回无波段影像，后续 select() 报 "Band pattern did not match"。用名称
    齐全、值全为空掩膜的占位影像替代，12 个月的堆叠结构永远稳定：缺失月份
    被 unmask(annualMedian) 用全年中值填补，或保留为 null（明确表示无观测）。
    该修复只在某月数据缺失时生效，数据齐全时与原版行为完全一致。
    """
    parts = [
        ee.Image.constant(0).updateMask(ee.Image.constant(0)).rename(name)
        for name in band_names
    ]
    return ee.Image.cat(parts)


def buildFeatureImage(year: int, aoi, cloud_pct: float) -> Tuple[ee.Image, ee.Image]:
    """构建全年 133 波段特征影像，返回 (finalImage, annualNDVI)。

    【平台修改】原生脚本的样本点集合是全局变量 samplePoints，这里把 AOI 与
    云量阈值参数化——每个空间块用自己块级的小 AOI 调用本函数构图，
    保证 GEE 每次求值只计算局部瓦片（交互式 getInfo 的 8GB 内存限制）。
    其余逻辑（月度合成、指数、归一化、年际中值填补、toBands 压平、
    {波段}_{月份} 命名、DEM）与原生脚本完全一致。
    """
    months = ee.List.sequence(1, 12)

    def process_month(m):
        m = ee.Number(m)
        startDate = ee.Date.fromYMD(year, m, 1)
        endDate = startDate.advance(1, 'month')

        s2_col = (ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
                  .filterBounds(aoi)
                  .filterDate(startDate, endDate)
                  .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', cloud_pct))
                  .map(maskS2clouds))
        # 该月无可用 S2 影像时用全掩膜占位，避免 select 波段名缺失报错
        s2 = ee.Image(ee.Algorithms.If(
            s2_col.size().gt(0), s2_col.median(), empty_named_image(S2_SOURCE_BANDS)))

        s1_col = (ee.ImageCollection('COPERNICUS/S1_GRD')
                  .filterBounds(aoi)
                  .filterDate(startDate, endDate)
                  .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VV'))
                  .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VH'))
                  .filter(ee.Filter.eq('instrumentMode', 'IW'))
                  .select(['VV', 'VH']))
        # 该月无可用 S1 影像时用全掩膜占位
        s1 = ee.Image(ee.Algorithms.If(
            s1_col.size().gt(0), s1_col.median(), empty_named_image(S1_SOURCE_BANDS)))

        evi = s2.expression(
            '2.5*((B8-B4)/(B8+6*B4-7.5*B2+1))',
            {
                'B8': s2.select('B8'),
                'B4': s2.select('B4'),
                'B2': s2.select('B2')
            }
        ).rename('EVI')

        combined = (s2.select(['B2', 'B3', 'B4', 'B8', 'B11', 'B12'])
                    .addBands(s2.normalizedDifference(['B8', 'B4']).rename('NDVI'))
                    .addBands(evi)
                    .addBands(s2.normalizedDifference(['B3', 'B8']).rename('NDWI'))
                    .addBands(s1))

        normalized_list = []
        for name, (min_v, max_v) in minMaxDict.items():
            normalized_list.append(normalizeBand(combined, name, min_v, max_v))

        return ee.Image.cat(normalized_list).set('month', m)

    # 12 个月的月度合成集合 + 全年中值（用于填补云遮挡导致的空值）
    monthlyImages = months.map(process_month)
    monthCol = ee.ImageCollection.fromImages(monthlyImages)
    annualMedian = monthCol.median()

    def fill_and_rename(img):
        img = ee.Image(img)
        # 【修改 1】：月份不补零的格式（例如 1, 12）
        mStr = ee.Number(img.get('month')).format('%d')
        unmasked = img.unmask(annualMedian)

        def rename_bands(b):
            # 【修改 2】：完全匹配老版本 CSV 格式：波段_月份（例如 VH_1, B2_12）
            return ee.String(b).cat('_').cat(mStr)

        new_names = img.bandNames().map(rename_bands)
        return unmasked.rename(new_names)

    filledImages = monthCol.map(fill_and_rename)

    # 利用 toBands() 压平并去掉数字前缀
    stackedTimeSeries = filledImages.toBands()

    def remove_prefix(name):
        return ee.String(name).replace('^[0-9]+_', '', 'r')

    new_names = stackedTimeSeries.bandNames().map(remove_prefix)
    stackedTimeSeries = stackedTimeSeries.rename(new_names)

    dem = normalizeBand(ee.Image('NASA/NASADEM_HGT/001'), 'elevation', 0, 2000)
    finalImage = stackedTimeSeries.addBands(dem)

    annualNDVI = monthCol.select('NDVI').median()
    return finalImage, annualNDVI


# =============================================================================
# 5. 特征提取：块级 AOI 构图 + 客户端小块 FC + 并行 getInfo
# =============================================================================
def extract_chunk(
    chunk_df: pd.DataFrame, lon_col: str, lat_col: str,
    label_property: str, scale: float, year: int, cloud_pct: float,
) -> Tuple[List[dict], int]:
    """提取一个空间块的样本特征（CSV 模式），返回 (属性字典列表, 本块点数)。

    【GEE 内存限制的关键经验——影像必须用块级 AOI 构图】
    - 用全样本 AOI 构图时，GEE 求值按全域栅格化/加载全域影像集，
      133 波段远超用户内存限制（实测：哪怕只采 250 点、只取 1 个月
      11 波段、tileScale 调到 2，都报 User memory limit exceeded）；
    - 用块级 AOI 构图后，每次求值只涉及块内小区域的瓦片与影像，
      实测 0.75° 范围 2000 点 133 波段稳定通过。
    块 FC 在客户端只构造块内的点（无需 inList 过滤，请求更小更快）。
    """
    chunk_fc = ee.FeatureCollection(features_from_dataframe(chunk_df, lon_col, lat_col))
    image, _ = buildFeatureImage(year, chunk_fc.geometry(), cloud_pct)
    sampled = image.sampleRegions(
        collection=chunk_fc,
        properties=[label_property, 'point_id'],
        scale=scale,
        tileScale=TILE_SCALE,
        geometries=False,
    )
    info = sampled.getInfo()
    props = [f["properties"] for f in info.get("features", [])]
    if len(props) != len(chunk_df):
        # 样本落在影像全掩膜区域时 sampleRegions 会整点丢失，明确告知而非静默少点
        print(
            f"  [警告] 一块应提取 {len(chunk_df)} 点，实际返回 {len(props)} 点"
            f"（部分样本可能位于影像无数据区域，结果表中对应行为空值）。"
        )
    return props, len(chunk_df)


def extract_chunk_from_asset(
    sample_fc, id_list: List[int], label_property: str,
    scale: float, year: int, cloud_pct: float,
) -> Tuple[List[dict], int]:
    """提取一个块的样本特征（资产 ID 模式）：服务端 inList 过滤 + 块级 AOI 构图。

    资产模式没有本地坐标，块 AOI 用过滤后子集的 geometry()（服务端先过滤
    再求几何，仍是块级 AOI）。注意：资产内点序若为散布，块的跨度可能超限，
    建议优先使用 CSV 入口。
    """
    subset = sample_fc.filter(ee.Filter.inList('point_id', ee.List(id_list)))
    image, _ = buildFeatureImage(year, subset.geometry(), cloud_pct)
    sampled = image.sampleRegions(
        collection=subset,
        properties=[label_property, 'point_id'],
        scale=scale,
        tileScale=TILE_SCALE,
        geometries=False,
    )
    info = sampled.getInfo()
    return [f["properties"] for f in info.get("features", [])], len(id_list)


def extract_all_features(chunks: List[List[int]], worker: Callable) -> List[dict]:
    """多线程并行提取各块样本特征，返回全部属性字典列表。

    worker(id_list) -> (属性字典列表, 本块点数)；块失败自动重试一次
    （瞬时网络错误），仍失败时抛出带块序号的错误；结果按块序拼接
    （CSV 模式最终按 point_id 合并回原表，顺序不影响正确性）。
    """
    n_chunks = len(chunks)

    def run_one(index: int, ids: List[int]):
        for attempt in range(2):
            try:
                return index, *worker(ids)
            except Exception as exc:
                if attempt == 0:
                    print(f"  [重试] 第 {index + 1}/{n_chunks} 块失败：{exc}")
                    time.sleep(5)
                else:
                    raise

    results: List[Tuple[int, List[dict]]] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(run_one, i, ids): i for i, ids in enumerate(chunks)}
        for fut in as_completed(futures):
            index = futures[fut]
            try:
                idx, props, count = fut.result()
                results.append((idx, props))
                print(f"  已提取 {len(results)}/{n_chunks} 块（第 {idx + 1} 块 {count} 点）。", flush=True)
            except Exception as exc:
                # 附加上下文：告诉用户是哪一块、什么规模的提取失败，方便定位
                raise ValueError(
                    f"第 {index + 1}/{n_chunks} 块（{len(chunks[index])} 个样本）GEE 提取失败，"
                    f"原始错误：{exc}"
                ) from exc

    results.sort(key=lambda t: t[0])
    props_list: List[dict] = []
    for _, props in results:
        props_list.extend(props)
    if not props_list:
        raise ValueError("GEE 未返回任何样本特征，请检查样本点位置是否在研究区内、影像是否有数据。")
    return props_list


# =============================================================================
# 6. 结果组装：特征表、波段统计、预览图、指标
# =============================================================================
def _json_rows(df: pd.DataFrame) -> List[list]:
    """DataFrame -> 平台表格行：NaN 转 None，numpy 标量转原生类型，保证 JSON 可序列化。"""
    rows = []
    for _, row in df.iterrows():
        cleaned = []
        for value in row:
            if isinstance(value, (np.floating,)):
                cleaned.append(None if not np.isfinite(value) else float(value))
            elif isinstance(value, (np.integer,)):
                cleaned.append(int(value))
            elif isinstance(value, (np.bool_,)):
                cleaned.append(bool(value))
            else:
                cleaned.append(value)
        rows.append(cleaned)
    return rows


def build_output_table(orig_df: Optional[pd.DataFrame], props_list: List[dict]) -> pd.DataFrame:
    """把 GEE 返回的属性列表组装成结果表；CSV 模式按 point_id 合并回原始表。"""
    feat_df = pd.DataFrame(props_list)
    if "point_id" in feat_df.columns:
        feat_df = feat_df.astype({"point_id": "int64"}, errors="ignore")

    if orig_df is None:
        return feat_df.drop(columns=["point_id"], errors="ignore")

    # GEE 返回的属性里包含标签列与 point_id，其余列以原始 CSV 为准，避免重复列
    overlap = [c for c in feat_df.columns if c in orig_df.columns and c != "point_id"]
    feat_only = feat_df.drop(columns=overlap)
    out = orig_df.merge(feat_only, on="point_id", how="left")
    return out.drop(columns=["point_id"])


def band_validity_table(df: pd.DataFrame, band_cols: List[str]) -> pd.DataFrame:
    """统计每个波段的有效样本数与有效比例，帮助下游建模时判断波段质量。"""
    records = []
    for band in band_cols:
        valid = df[band].notna()
        records.append({
            "波段": band,
            "有效样本数": int(valid.sum()),
            "有效比例(%)": round(float(valid.mean()) * 100, 2),
        })
    return pd.DataFrame(records)


def region_and_scale_from_bounds(minx: float, miny: float, maxx: float, maxy: float) -> Tuple[dict, int]:
    """由坐标范围计算预览图下载区域与分辨率（最长边约 2000 像元，控制文件大小）。"""
    pad = 0.02
    minx, maxx = minx - pad, maxx + pad
    miny, maxy = miny - pad, maxy + pad
    dx = max(maxx - minx, 0.05)
    dy = max(maxy - miny, 0.05)
    # 1° ≈ 111.32 km，最长边限制在 2000 像元
    scale = max(10, int(round(max(dx, dy) * 111320 / 2000)))
    region = {
        "type": "Polygon",
        "coordinates": [[[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]],
    }
    return region, scale


def export_ndvi_preview(ndvi_image, region: dict, scale: int, workdir: Path) -> Optional[Path]:
    """通过 getDownloadURL 直接下载全年 NDVI 中值预览 GeoTIFF（不走 Drive）。

    属于锦上添花的输出，任何失败（网络、权限等）只打印警告并跳过，
    不影响特征提取主结果。
    """
    try:
        url = ndvi_image.getDownloadURL({
            "name": "annual_ndvi_preview",
            "region": region,
            "scale": scale,
            "crs": "EPSG:4326",
            "format": "GEO_TIFF",
        })
        tif_path = workdir / "全年NDVI中值预览.tif"
        urllib.request.urlretrieve(url, str(tif_path))
        print(f"  预览图已保存: {tif_path}")
        return tif_path
    except Exception as exc:
        print(f"  [警告] NDVI 预览图生成失败（不影响特征提取结果）：{exc}")
        return None


# =============================================================================
# 7. 平台接入：ALGO_META 与 run()
# =============================================================================
# FastAPI 平台约定：每个算法文件暴露 ALGO_META（前端据此生成参数表单）
# 和 run(inputs, workdir)。inputs 的值是参数名到服务器端文件路径（或标量）的映射，
# workdir 是本次运行的专属输出目录 outputs/{run_id}/。

ALGO_META = {
    "id": "future2",
    "name": "GEE 全时序遥感特征提取（波段_月份命名）",
    "description": (
        "基于 Python-GEE 接口在 Earth Engine 服务器端提取样本点全年遥感特征："
        "12 个月的 Sentinel-2 光谱与指数（B2/B3/B4/B8/B11/B12/NDVI/EVI/NDWI）、"
        "Sentinel-1 VV/VH 后向散射，经云掩膜、月内中值合成、0~1 归一化、"
        "年际中值填补后堆叠，再加归一化地形高程，共 133 个波段，"
        "波段命名为「波段_月份」（如 VH_1、B2_12），输出带标签的样本特征 CSV "
        "供下游机器学习使用。"
        "样本点 CSV 需含经度/纬度列（支持 Longitude/Lon/X/经度 与 Latitude/Lat/Y/纬度），"
        "其余列自动保留到结果表；无需把 CSV 上传成 GEE 资产，也可填写已有表资产 ID。"
        "GEE 凭证默认使用本机 earthengine authenticate 登录信息，"
        "部署到服务器时建议上传服务账号 JSON 凭证。"
    ),
    "params": [
        {"name": "sample_csv", "label": "样本点坐标 CSV", "type": "csv", "required": False},
        {
            "name": "label_property",
            "label": "标签属性列名",
            "type": "text",
            "required": True,
            "default": "class",
        },
        {"name": "year", "label": "提取年份", "type": "number", "required": True, "default": 2025},
        {"name": "cloud_pct", "label": "云量阈值（%）", "type": "number", "required": False, "default": 80},
        {"name": "sample_scale", "label": "采样分辨率（米）", "type": "number", "required": False, "default": 10},
        {"name": "gee_project", "label": "GEE 项目 ID", "type": "text", "required": False, "default": DEFAULT_PROJECT},
        {"name": "gee_credentials", "label": "GEE 服务账号 JSON", "type": "json", "required": False},
        {"name": "gee_table_asset", "label": "已有 GEE 表资产 ID", "type": "text", "required": False},
    ],
}


def run(inputs: dict, workdir: Path) -> dict:
    """平台约定入口：解析参数 → 初始化 GEE → 样本点 → 分块提取 → 组装返回结构。"""
    workdir = Path(workdir)
    # 平台正常会创建输出目录，但直接调用（测试/复用）时可能不存在，这里兜底创建
    workdir.mkdir(parents=True, exist_ok=True)

    # ---------- 1. 参数解析（含默认值） ----------
    project = str(inputs.get("gee_project") or DEFAULT_PROJECT).strip()
    year = int(round(float(inputs.get("year") or DEFAULT_YEAR)))
    cloud_pct = float(inputs.get("cloud_pct") or DEFAULT_CLOUD_PCT)
    sample_scale = float(inputs.get("sample_scale") or DEFAULT_SCALE)
    label_property = str(inputs.get("label_property") or "class").strip()
    asset_id = str(inputs.get("gee_table_asset") or "").strip()
    sample_csv = inputs.get("sample_csv")
    credentials_file = inputs.get("gee_credentials")

    if sample_csv is None and not asset_id:
        raise ValueError("必须提供「样本点坐标 CSV」或「已有 GEE 表资产 ID」之一。")
    if not 2014 <= year <= 2030:
        raise ValueError(f"提取年份 {year} 超出支持范围（2014-2030）。")
    if not 0 <= cloud_pct <= 100:
        raise ValueError(f"云量阈值 {cloud_pct} 须在 0-100 之间。")
    if not 5 <= sample_scale <= 100:
        raise ValueError(f"采样分辨率 {sample_scale} 须在 5-100 米之间。")

    # ---------- 2. GEE 初始化 ----------
    init_gee(project, credentials_file)

    # ---------- 3. 样本点 → 分块 + 提取任务 ----------
    if asset_id:
        sample_fc = ee.FeatureCollection(asset_id)
        orig_df = None
        n = int(sample_fc.size().getInfo())
        sample_fc = add_point_ids(sample_fc, n)
        # 资产模式没有本地坐标，无法按空间聚类，只能按 point_id 顺序切块；
        # 若资产表内点序恰好散布，块跨度可能超限，建议优先使用 CSV 入口。
        chunks = [
            list(range(s, min(s + CHUNK_SIZE, n)))
            for s in range(0, n, CHUNK_SIZE)
        ]
        worker = lambda ids: extract_chunk_from_asset(
            sample_fc, ids, label_property, sample_scale, year, cloud_pct
        )
    else:
        orig_df, lon_col, lat_col, dropped = parse_sample_csv(Path(sample_csv), label_property)
        if dropped:
            print(f"已跳过 {dropped} 行坐标无效/越界的样本。")
        n = len(orig_df)
        # 按网格聚成空间连续、面积受控的分块（关键：每块影像用块级 AOI 构图，
        # 避免 GEE 单次求值内存超限，详见 build_spatial_chunks / extract_chunk）
        chunks = build_spatial_chunks(orig_df, lon_col, lat_col)
        worker = lambda ids: extract_chunk(
            orig_df[orig_df["point_id"].isin(ids)], lon_col, lat_col,
            label_property, sample_scale, year, cloud_pct,
        )
    print(f"样本点数: {n}；提取年份: {year}；云量阈值: {cloud_pct}%；采样分辨率: {sample_scale} m")

    # ---------- 4+5. 分块构图并并行提取样本特征 ----------
    print(f"正在从 GEE 提取样本特征（{len(chunks)} 个空间分块，{MAX_WORKERS} 线程并行）…")
    props_list = extract_all_features(chunks, worker)

    # ---------- 6. 组装结果表 ----------
    out_df = build_output_table(orig_df, props_list)
    band_cols = [c for c in out_df.columns if c == DEM_BAND or BAND_COL_PATTERN.match(str(c))]

    feature_csv = workdir / "样本特征表.csv"
    out_df.to_csv(feature_csv, index=False, encoding="utf-8-sig")

    validity_df = band_validity_table(out_df, band_cols)
    validity_csv = workdir / "波段有效性统计.csv"
    validity_df.to_csv(validity_csv, index=False, encoding="utf-8-sig")

    # ---------- 7. 预览图（可选，失败降级） ----------
    rasters = []
    print("正在生成全年 NDVI 中值预览图…")
    # 预览图的 NDVI 合成用样本外包矩形构图：下载分辨率粗（数百米），内存不受影响
    if orig_df is not None:
        minx = float(orig_df[lon_col].min())
        maxx = float(orig_df[lon_col].max())
        miny = float(orig_df[lat_col].min())
        maxy = float(orig_df[lat_col].max())
        region, preview_scale = region_and_scale_from_bounds(minx, miny, maxx, maxy)
        aoi_box = ee.Geometry.Rectangle([minx, miny, maxx, maxy])
    else:
        bounds = sample_fc.geometry().bounds().getInfo()
        coords = bounds["coordinates"][0]
        region, preview_scale = region_and_scale_from_bounds(
            min(pt[0] for pt in coords), min(pt[1] for pt in coords),
            max(pt[0] for pt in coords), max(pt[1] for pt in coords),
        )
        aoi_box = sample_fc.geometry().bounds()
    _, ndvi_annual = buildFeatureImage(year, aoi_box, cloud_pct)
    preview_tif = export_ndvi_preview(ndvi_annual, region, preview_scale, workdir)
    if preview_tif is not None:
        rasters.append({"name": "全年 NDVI 中值合成预览", "tif": str(preview_tif)})

    # ---------- 8. 指标 ----------
    def push_metric(name, value, unit="", decimals=3):
        if isinstance(value, float) and not np.isfinite(value):
            return None
        return {"name": name, "value": float(value), "unit": unit, "decimals": decimals}

    valid_mask = out_df[band_cols].notna()
    valid_ratio = float(valid_mask.mean().mean()) if len(out_df) else 0.0
    avg_bands = float(valid_mask.sum(axis=1).mean()) if len(out_df) else 0.0
    full_valid = int(valid_mask.all(axis=1).sum())
    label_count = int(out_df[label_property].nunique()) if label_property in out_df.columns else 0

    metrics = [
        m for m in [
            push_metric("样本点数量", len(out_df), "个", 0),
            push_metric("特征波段数", len(band_cols), "个", 0),
            push_metric("平均有效波段数", avg_bands, "个", 1),
            push_metric("特征有效值占比", valid_ratio * 100, "%", 2),
            push_metric("全波段有效样本数", full_valid, "个", 0),
            push_metric("标签类别数", label_count, "类", 0),
        ] if m
    ]

    # Web 端只回传前 N 行（15000×136 全量 JSON 约 40MB），完整数据走 csv 下载
    web_rows = _json_rows(out_df.head(WEB_PREVIEW_ROWS))
    tables = [
        {
            "name": f"样本特征表（共 {len(out_df)} 行 × {len(out_df.columns)} 列，页面预览前 {WEB_PREVIEW_ROWS} 行）",
            "columns": list(out_df.columns),
            "rows": web_rows,
            "file": str(feature_csv),
        },
        {
            "name": "波段有效性统计",
            "columns": list(validity_df.columns),
            "rows": _json_rows(validity_df),
            "file": str(validity_csv),
        },
    ]

    message = (
        f"{year} 年 {n} 个样本点完成 133 波段时序特征提取："
        f"平均每样本 {avg_bands:.1f} 个有效波段（{valid_ratio * 100:.2f}%），"
        f"结果已保存为样本特征表 CSV。"
    )
    return {"message": message, "metrics": metrics, "tables": tables, "rasters": rasters}
