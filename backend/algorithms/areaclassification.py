# -*- coding: utf-8 -*-
from __future__ import annotations

"""
GEE 随机森林区域分类算法（平台适配版）：输入第一步导出的样本特征 CSV 与研究区
矢量边界，输出 10 m 研究区 5 波段类别概率图 + 单波段分类图（WGS84）。

本文件 = 原生 areaclassification.py 算法核心（随机森林分类，原样保留）
      + 平台接入层（ALGO_META / run()）。

与原生脚本的差异（都在算法核心之外）：
  1. 原生依赖两个 GEE 资产（table1 样本特征表、table2 矢量边界），本版本改为
     直接上传第一步导出的样本特征 CSV（含经纬度、标签与 133 个特征列，客户端
     构造训练 FeatureCollection）与研究区矢量边界 shp 压缩包（本地解析并栅格化
     边界掩膜），无需上传任何 GEE 资产。
  2. 原生走 Drive 批量导出，本版本同步返回结果：GEE 下载接口单次上限 32 MB、
     网格边长上限 10000，因此把研究区切成 ≤2200×2200 的瓦片，用 crsTransform
     精确指定每块瓦片的像素网格（GEE 按请求输出，瓦片拼接无缝、无需猜测其
     默认网格对齐规则），多线程并行下载后本地合并。研究区大小不设硬上限：
     完全位于矢量边界之外的瓦片直接跳过（省/市级尺度约省一半下载量），
     大研究区只打印耗时提示，合并完成后删除瓦片缓存以释放磁盘。
  3. 输出与原版界面描述一致：5 波段类别概率图，波段顺序 = 类别编码升序
     （本项目编码 10/20/30/40/50 对应 甘蔗/水稻/桉树/其他作物/背景，值 0-100，
     Byte 存储以把下载量压缩到 1/4）；另派生单波段分类图（类别编码）与
     类别面积统计表。分辨率默认 10 m，坐标系统 WGS84（EPSG:4326）。
  4. 概率图用 MULTIPROBABILITY 输出模式一次分类得到（当前 GEE 后端该模式
     classify() 输出为单波段数组，需 arrayGet 展开成每类一个波段），分类图
     本地按 argmax 派生，只下载一份影像；像素在矢量边界之外时用本地掩膜
     置 0 / nodata。

脚本结构：
  1. 全局配置：默认参数、下载瓦片/像元上限、类别配色。
  2. 复用 future2：init_gee（GEE 初始化）、buildFeatureImage（133 波段特征
     影像构建，保证与第一步 CSV 的「波段_月份」命名完全一致）。
  3. 训练表解析与清洗：特征波段识别、标签编码、无效行剔除、可选分层抽样。
  4. 矢量边界读取：shp 压缩包 → WGS84 并集几何 → 下载网格规划。
  5. 训练与分类：客户端 FeatureCollection 训练随机森林（MULTIPROBABILITY），
     对重建的特征影像逐像元分类，概率 ×100 转 Byte。
  6. 瓦片下载：crsTransform 精确网格 + 多线程并行 + 失败重试 + 断点续传，
     边界外瓦片自动跳过。
  7. 结果合并：概率图 GeoTIFF、分类图（argmax + 边界掩膜）、类别统计表。
  8. 平台接入：ALGO_META 参数声明 + run() 入口。
"""

import math
import shutil
import sys
import time
import urllib.request
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

import ee
import rasterio
from rasterio.features import rasterize
from rasterio.transform import Affine
from rasterio.windows import Window

# 把本文件所在目录加入 sys.path，保证被平台 registry（任意工作目录）加载时
# 仍能 import 同目录辅助模块（future2 为第一步算法，特征影像构建直接复用）。
_ALGO_DIR = Path(__file__).resolve().parent
if str(_ALGO_DIR) not in sys.path:
    sys.path.insert(0, str(_ALGO_DIR))

import future2
from future2 import (
    BAND_COL_PATTERN,
    DEFAULT_PROJECT,
    DEM_BAND,
    MONTHLY_BANDS,
    buildFeatureImage,
    init_gee,
    parse_sample_csv,
)

# =============================================================================
# 1. 全局配置
# =============================================================================
DEFAULT_YEAR = 2025
DEFAULT_CLOUD_PCT = 80.0
DEFAULT_SCALE = 10.0
DEFAULT_NUM_TREES = 100

# 默认类别名称映射：编码 → 名称（本项目 10/20/30/40/50 = 甘蔗/水稻/桉树/其他作物/背景）。
# 未映射到的编码自动命名为「类别{编码}」；也可通过「类别名称」参数整体覆盖。
DEFAULT_CLASS_NAMES = ["甘蔗", "水稻", "桉树", "其他作物", "背景"]
DEFAULT_CLASS_MAP = {10: "甘蔗", 20: "水稻", 30: "桉树", 40: "其他作物", 50: "背景"}

# 分类图预览配色（与类别编码升序对应，颜色数不足时自动截取）
CLASS_COLORS = ["#f16913", "#238b45", "#004529", "#fec44f", "#bdbdbd"]

# WGS84 单位转换：EPSG:4326 名义 1° = 111319.49079327358 m，
# 即 GEE 把 scale（米）换算成度数时的基准（同一格网经纬度向与纬度向同尺寸）。
METERS_PER_DEG = 111319.49079327358

# 每块下载瓦片的像元数上限：GEE 下载接口单次最多 32 MB，5 波段 Byte 影像
# 2200×2200×5 ≈ 24.2 MB，留足余量；单边 10000 是 GEE 网格硬上限。
TILE_MAX_DIM = 10000
TILE_MAX_PX = 2200 * 2200

# 大研究区提示阈值：超过该像元规模只打印耗时提示，不再拒绝运行。
# 下载按 ≤2200×2200 瓦片流式进行，总量不设硬上限；同步接口下大型任务
# 会长时间阻塞（可能数小时），需保持后端运行与前端页面不关闭。
HUGE_JOB_WARN_PX = 200_000_000

# 瓦片并行下载线程数与单瓦片失败重试次数
MAX_WORKERS = 8
DOWNLOAD_RETRIES = 2

# 合并完成后删除瓦片缓存：省/市级尺度 10 m 运行的瓦片可达数十 GB，
# 最终 GeoTIFF 已包含全部结果，瓦片仅用于下载阶段断点续传。
DELETE_TILES_AFTER_MERGE = True

# 分类图（Byte）的 nodata 值；类别编码必须 ≤ 250，见 run() 的校验
CLASS_NODATA = 255

# 第一步特征影像的全部 133 个波段名（顺序即影像波段顺序）
EXPECTED_BANDS = [f"{b}_{m}" for m in range(1, 13) for b in MONTHLY_BANDS] + [DEM_BAND]


# =============================================================================
# 2. 训练表解析与清洗
# =============================================================================
def resolve_feature_bands(columns) -> Tuple[List[str], List[str]]:
    """确定训练特征波段：CSV 列与特征影像波段（133 个）取交集。

    返回 (使用的特征波段, CSV 中缺失的波段)。只训练 CSV 里实际存在的波段，
    分类时 select 同样的波段，保证训练与分类口径一致。
    """
    cols = [str(c) for c in columns]
    present = [b for b in EXPECTED_BANDS if b in cols]
    missing = [b for b in EXPECTED_BANDS if b not in cols]
    return present, missing


def pair_class_names(classes: List[int], class_names_param: str) -> List[str]:
    """类别名称与编码配对，支持两种写法：

      - 「编码:名称」逗号分隔（如 10:甘蔗,20:水稻）：按编码精确映射，
        未映射的类别自动命名为「类别{编码}」；样本只含部分类别时也不会错位；
      - 纯名称逗号分隔：按编码升序一一对应，名称不够时用「类别{编码}」。
    参数为空时用默认映射（10/20/30/40/50 = 甘蔗/水稻/桉树/其他作物/背景）。
    """
    given = [s.strip() for s in (class_names_param or "").split(",") if s.strip()]
    if not given:
        return [DEFAULT_CLASS_MAP.get(c, f"类别{c}") for c in classes]
    code_map = {}
    for item in given:
        if ":" not in item:
            continue
        code_s, name = item.split(":", 1)
        try:
            code_map[int(float(code_s.strip()))] = name.strip()
        except ValueError:
            continue
    if code_map:
        return [code_map.get(c, f"类别{c}") for c in classes]
    return [given[i] if i < len(given) else f"类别{c}" for i, c in enumerate(classes)]


def subsample_train(df: pd.DataFrame, label_property: str, max_points: int, seed: int = 42) -> pd.DataFrame:
    """训练样本超过上限时按类别比例分层抽样，每个类别至少保留 1 个样本。

    上限用于控制客户端 FeatureCollection 的请求体积（GEE 请求载荷有限），
    默认 0 = 全部样本（与原生一致）。
    """
    n = len(df)
    if n <= max_points:
        return df
    counts = df[label_property].value_counts().sort_index()
    take = (counts / n * max_points).round().clip(lower=1).astype(int)
    while int(take.sum()) > max_points:
        i = take[take > 1].idxmax()
        take[i] -= 1
    parts = []
    for cls, group in df.groupby(label_property, sort=True):
        k = int(take.get(cls, 1))
        parts.append(group.sample(n=min(k, len(group)), random_state=seed))
    return pd.concat(parts).reset_index(drop=True)


def build_training_features(
    df: pd.DataFrame, lon_col: str, lat_col: str,
    label_property: str, feature_bands: List[str],
) -> ee.FeatureCollection:
    """构造训练 FeatureCollection（仅标签 + 特征波段属性）。

    属性数值保留 6 位小数：GEE 客户端序列化双精度浮点最多 17 位，
    6 位小数即可保证精度，同时把请求体积压缩近一半。
    """
    feats = []
    for _, row in df.iterrows():
        props = {label_property: int(row[label_property])}
        for band in feature_bands:
            v = row[band]
            if pd.isna(v):
                continue  # 理论上清洗后不存在，防御性跳过
            props[band] = round(float(v), 6)
        feats.append(
            ee.Feature(ee.Geometry.Point([float(row[lon_col]), float(row[lat_col])]), props)
        )
    return ee.FeatureCollection(feats)


def build_probability_image(
    final_image, feature_bands: List[str], train_fc: ee.FeatureCollection,
    label_property: str, num_trees: int, n_classes: int,
) -> ee.Image:
    """训练随机森林并对特征影像逐像元分类，返回概率影像（每类一个波段，×100 转 Byte）。

    【GEE 后端行为（实测）】MULTIPROBABILITY 输出模式的 classify() 结果是单波段
    「classification」，像元值为长度 = 类别数的数组（元素顺序 = 类别编码升序），
    必须用 arrayGet 按位取出展开成独立波段；越界索引会直接报错，因此波段数
    严格等于训练集类别数。概率 ×100 后转 Byte：下载体积为 float32 的 1/4。
    """
    rf = (
        ee.Classifier.smileRandomForest(num_trees)
        .setOutputMode("MULTIPROBABILITY")
        .train(
            features=train_fc,
            classProperty=label_property,
            inputProperties=feature_bands,
        )
    )
    arr = final_image.select(feature_bands).classify(rf).select("classification")
    prob_bands = [
        arr.arrayGet(ee.Image(i)).rename(f"prob_{i}") for i in range(n_classes)
    ]
    return ee.Image.cat(prob_bands).multiply(100).toByte()


# =============================================================================
# 3. 矢量边界读取与下载网格规划
# =============================================================================
def extract_zip_safe(zip_path: Path, outdir: Path) -> Path:
    """把压缩包安全解压到指定目录（跳过绝对路径与 .. 成员）。"""
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = Path(info.filename)
            if name.is_absolute() or ".." in name.parts:
                continue
            z.extract(info, outdir)
    return outdir


def read_boundary(boundary_zip: Path, workdir: Path):
    """读取矢量边界 shp 压缩包，返回 WGS84 下的并集几何与外包矩形。

    geopandas 直接读 zip:// URI 在 Windows 绝对路径下有兼容性问题，
    统一解压到 workdir 下再读，结果也更便于排查。
    """
    unzip_dir = workdir / "_边界矢量"
    extract_zip_safe(boundary_zip, unzip_dir)
    shp_files = sorted(unzip_dir.rglob("*.shp"))
    if not shp_files:
        raise ValueError("边界压缩包内未找到 .shp 文件。")
    import geopandas as gpd

    gdf = gpd.read_file(shp_files[0])
    if gdf.crs is None:
        raise ValueError("矢量边界缺少坐标系定义（.prj），无法确定投影。")
    gdf = gdf.to_crs("EPSG:4326")
    gdf = gdf[gdf.geometry.notna()].copy()
    if gdf.empty:
        raise ValueError("矢量边界没有有效几何。")
    geom = gdf.dissolve().geometry.iloc[0]
    if geom is None or geom.is_empty:
        raise ValueError("矢量边界没有有效几何。")
    minx, miny, maxx, maxy = geom.bounds
    if not all(np.isfinite([minx, miny, maxx, maxy])):
        raise ValueError("矢量边界坐标越界（应位于 WGS84 经纬度范围内）。")
    return geom, (minx, miny, maxx, maxy)


def plan_download_grid(bounds: Tuple[float, float, float, float], scale: float, geom):
    """由研究区外包矩形规划全局像素网格与下载瓦片，返回 (pixel, nx, ny, tiles)。

    网格原点定在外包矩形左上角，瓦片边界取像素尺寸的整倍数，保证每块瓦片
    crsTransform 严格落在全局网格上，拼接无缝。像元总量不设上限：完全位于
    矢量边界之外的瓦片标记 download=False 直接跳过（不下载，合并时按
    0/nodata 写入），不规则边界（如省界）可省去近半下载与 GEE 计算量。
    """
    from shapely.geometry import box as shapely_box
    from shapely.prepared import prep

    prepared = prep(geom)
    minx, miny, maxx, maxy = bounds
    pixel = scale / METERS_PER_DEG
    nx = max(1, int(np.ceil((maxx - minx) / pixel - 1e-9)))
    ny = max(1, int(np.ceil((maxy - miny) / pixel - 1e-9)))
    side = min(TILE_MAX_DIM, int(math.sqrt(TILE_MAX_PX)))
    cols = int(math.ceil(nx / side))
    rows = int(math.ceil(ny / side))
    tiles = []
    for r in range(rows):
        for c in range(cols):
            w = min(side, nx - c * side)
            h = min(side, ny - r * side)
            x0 = minx + c * side * pixel
            y1 = maxy - r * side * pixel
            tiles.append({
                "row": r, "col": c,
                "col_off": c * side, "row_off": r * side,
                "x0": x0, "y1": y1,
                "w": w, "h": h,
                # 瓦片窗口与边界多边形不相交 → 像元全部在边界外，跳过下载
                "download": prepared.intersects(
                    shapely_box(x0, y1 - h * pixel, x0 + w * pixel, y1)
                ),
            })
    if not any(t["download"] for t in tiles):
        raise ValueError("研究区边界与外包矩形无交集，没有需要下载的瓦片。")
    return pixel, nx, ny, tiles


# =============================================================================
# 4. 瓦片下载
# =============================================================================
def download_tile(prob_image, tile: dict, pixel: float, n_bands: int, tile_dir: Path) -> Path:
    """下载一块瓦片（crsTransform 精确指定网格，format GEO_TIFF）。

    crsTransform + dimensions 给定后 region 会被 GEE 忽略，输出严格等于
    请求的网格窗口；断点续传：已存在且尺寸正确的瓦片直接复用。
    """
    path = tile_dir / f"r{tile['row']:03d}_c{tile['col']:03d}.tif"
    if path.is_file():
        try:
            with rasterio.open(path) as src:
                if src.count == n_bands and src.width == tile["w"] and src.height == tile["h"]:
                    return path
        except Exception:
            pass  # 损坏的旧文件，重新下载

    params = {
        "name": path.stem,
        "crs": "EPSG:4326",
        "crsTransform": [pixel, 0, 0, -pixel, tile["x0"], tile["y1"]],
        "dimensions": [tile["w"], tile["h"]],
        "format": "GEO_TIFF",
    }
    url = prob_image.getDownloadURL(params)
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=1800) as resp:
            content = resp.read()  # 单瓦片 ≤ 24 MB，可直接读入内存
    except urllib.error.HTTPError as exc:
        # GEE 下载服务器拒绝时在响应体里给出原因（如网格过大/带宽限制），
        # 取前 500 字符随异常带出，方便定位 400 等错误。
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.code} {exc.reason}: {body}") from exc
    path.write_bytes(content)

    # 立即校验下载结果，失败时让外层重试而不是等到合并阶段才发现
    with rasterio.open(path) as src:
        if src.count != n_bands or src.width != tile["w"] or src.height != tile["h"]:
            raise ValueError(f"GEE 返回的瓦片尺寸 {src.width}×{src.height} 与请求不符")
    return path


def download_all_tiles(prob_image, tiles: List[dict], pixel: float, n_bands: int, tile_dir: Path) -> None:
    """多线程并行下载需要下载的瓦片；单瓦片失败自动重试，仍失败时抛出带位置的错误。"""
    targets = [t for t in tiles if t.get("download", True)]
    skipped = len(tiles) - len(targets)
    if skipped:
        print(f"  已跳过 {skipped} 个完全位于研究区边界之外的瓦片（共 {len(tiles)} 个）。")
    n = len(targets)

    def run_one(tile: dict):
        last_exc: Optional[Exception] = None
        for attempt in range(DOWNLOAD_RETRIES + 1):
            try:
                return download_tile(prob_image, tile, pixel, n_bands, tile_dir)
            except Exception as exc:
                last_exc = exc
                if attempt < DOWNLOAD_RETRIES:
                    print(f"  [重试] 瓦片 r{tile['row']}c{tile['col']} 下载失败：{exc}")
                    time.sleep(5 * (attempt + 1))
        raise last_exc

    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(run_one, t): t for t in targets}
        for fut in as_completed(futures):
            tile = futures[fut]
            try:
                fut.result()
            except Exception as exc:
                raise ValueError(
                    f"瓦片 r{tile['row']}c{tile['col']}（{tile['w']}×{tile['h']} 像元）"
                    f"下载失败，原始错误：{exc}"
                ) from exc
            done += 1
            print(f"  已下载 {done}/{n} 个瓦片。", flush=True)


# =============================================================================
# 5. 瓦片合并：概率图 + 分类图 + 类别统计
# =============================================================================
def _same_transform(a: Affine, b: Affine, tol: float = 1e-9) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(tuple(a)[:6], tuple(b)[:6]))


def merge_tiles(
    workdir: Path, tiles: List[dict], pixel: float, nx: int, ny: int,
    bounds: Tuple[float, float, float, float], geom, class_codes: List[int], class_names: List[str],
) -> Tuple[Path, Path, dict]:
    """把瓦片按窗口写入最终结果，返回 (概率图路径, 分类图路径, 类别像元统计)。

    概率图 = 瓦片原样拼接；分类图 = 概率 argmax 得到类别编码，再套研究区
    边界掩膜（边界外置 nodata）；统计只在掩膜内的像元上累计。
    """
    minx, miny, maxx, maxy = bounds
    n_bands = len(class_codes)
    codes = np.array(class_codes, dtype="int64")
    global_tf = Affine(pixel, 0, minx, 0, -pixel, maxy)

    prob_tif = workdir / "研究区分类概率图.tif"
    class_tif = workdir / "研究区分类图.tif"
    profile = dict(
        driver="GTiff", width=nx, height=ny, dtype="uint8", crs="EPSG:4326",
        transform=global_tf, compress="deflate", predictor=2,
        tiled=True, blockxsize=256, blockysize=256, BIGTIFF="IF_SAFER",
    )
    class_counts: dict = defaultdict(int)
    with rasterio.open(prob_tif, "w", count=n_bands, **profile) as prob_dst, \
            rasterio.open(class_tif, "w", count=1, nodata=CLASS_NODATA, **profile) as cls_dst:
        for i, name in enumerate(class_names):
            prob_dst.set_band_description(i + 1, name)
        cls_dst.set_band_description(1, "类别编码")

        from shapely.geometry import box as shapely_box

        for tile in tiles:
            window = Window(tile["col_off"], tile["row_off"], tile["w"], tile["h"])
            if not tile.get("download", True):
                # 未下载的瓦片完全在边界外：概率图写 0、分类图写 nodata，不读瓦片文件
                prob_dst.write(
                    np.zeros((n_bands, tile["h"], tile["w"]), dtype="uint8"),
                    window=window,
                )
                cls_dst.write(
                    np.full((tile["h"], tile["w"]), CLASS_NODATA, dtype="uint8"),
                    1, window=window,
                )
                continue
            tile_path = workdir / "tiles" / f"r{tile['row']:03d}_c{tile['col']:03d}.tif"
            expected_tf = Affine(pixel, 0, tile["x0"], 0, -pixel, tile["y1"])
            with rasterio.open(tile_path) as src:
                if src.count != n_bands or src.width != tile["w"] or src.height != tile["h"]:
                    raise ValueError(f"瓦片 {tile_path.name} 尺寸/波段数与预期不一致。")
                if not _same_transform(src.transform, expected_tf):
                    raise ValueError(
                        f"瓦片 {tile_path.name} 网格与预期不一致（GEE 未按请求的 "
                        f"crsTransform 输出），拼接会产生错位，请重试。"
                    )
                data = src.read()  # (n_bands, h, w)

            # 边界掩膜：只把瓦片窗口内的边界部分裁剪出来再栅格化（与全局网格
            # 同格网，逐窗等价）；大边界逐瓦片裁剪比整幅栅格化快一个量级。
            tile_geom = geom.intersection(shapely_box(
                tile["x0"], tile["y1"] - tile["h"] * pixel,
                tile["x0"] + tile["w"] * pixel, tile["y1"],
            ))
            shapes = [(tile_geom, 1)] if not tile_geom.is_empty else []
            mask = rasterize(
                shapes,
                out_shape=(tile["h"], tile["w"]),
                transform=expected_tf,
                all_touched=True, fill=0, dtype="uint8",
            )
            data[:, mask == 0] = 0
            cls = codes[np.argmax(data, axis=0)]
            cls = np.where(mask > 0, cls, CLASS_NODATA).astype("uint8")

            prob_dst.write(data, window=window)
            cls_dst.write(cls, 1, window=window)

            uniq, cnt = np.unique(cls[cls != CLASS_NODATA], return_counts=True)
            for code, k in zip(uniq.tolist(), cnt.tolist()):
                class_counts[int(code)] += int(k)
    return prob_tif, class_tif, dict(class_counts)


def build_class_stats(
    class_counts: dict, class_codes: List[int], class_names: List[str],
    scale: float, bounds: Tuple[float, float, float, float],
) -> Tuple[pd.DataFrame, int]:
    """类别像元统计 → 统计表（面积按格网纬度换算，标注近似）。"""
    lat_center = (bounds[1] + bounds[3]) / 2
    px_area_km2 = (scale / 1000.0) ** 2 * math.cos(math.radians(lat_center))
    total = sum(class_counts.values())
    rows = []
    for code, name in zip(class_codes, class_names):
        cnt = int(class_counts.get(int(code), 0))
        rows.append({
            "类别": name,
            "编码": int(code),
            "像元数": cnt,
            "面积(km²,近似)": round(cnt * px_area_km2, 3),
            "占比(%)": round(cnt / total * 100, 2) if total else 0.0,
        })
    return pd.DataFrame(rows), int(total)


# =============================================================================
# 6. 分类图预览 PNG（离散配色 + 图例）
# =============================================================================
def render_class_preview(
    class_tif: Path, png_path: Path, class_names: List[str], class_codes: List[int], title: str,
) -> Path:
    """把分类图渲染成带类别图例的 PNG 预览（大影像自动降采样）。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from matplotlib.patches import Patch

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
    plt.rcParams["axes.unicode_minus"] = False

    with rasterio.open(class_tif) as src:
        step = max(1, max(src.width, src.height) // 1024 + 1)
        arr = src.read(1, out_shape=(src.height // step, src.width // step)).astype("float64")
    arr[arr == CLASS_NODATA] = np.nan

    n = len(class_codes)
    cmap = ListedColormap(CLASS_COLORS[:n])
    bounds = [c - 0.5 for c in class_codes] + [class_codes[-1] + 0.5]
    norm = BoundaryNorm(bounds, cmap.N)

    fig, ax = plt.subplots(figsize=(6.4, 5.2), dpi=110)
    ax.imshow(arr, cmap=cmap, norm=norm, interpolation="nearest")
    handles = [Patch(color=CLASS_COLORS[i], label=name) for i, name in enumerate(class_names)]
    ax.legend(handles=handles, loc="lower right", fontsize=8, framealpha=0.9)
    ax.set_title(title)
    ax.set_axis_off()
    fig.tight_layout()
    fig.savefig(png_path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return png_path


# =============================================================================
# 7. 平台接入：ALGO_META 与 run()
# =============================================================================
# FastAPI 平台约定：每个算法文件暴露 ALGO_META（前端据此生成参数表单）
# 和 run(inputs, workdir)。inputs 的值是参数名到服务器端文件路径（或标量）的映射，
# workdir 是本次运行的专属输出目录 outputs/{run_id}/。

ALGO_META = {
    "id": "areaclassification",
    "name": "GEE 随机森林区域分类（作物分类图）",
    "description": (
        "研究区作物分类：输入第一步特征提取导出的样本特征 CSV（含经纬度、标签列与 "
        "133 个特征列）和研究区矢量边界（shp 压缩包 zip），在 GEE 上按同一口径重建 "
        "全年 133 波段特征影像，训练随机森林后对整个研究区逐像元分类。"
        "输出（同步返回，无需 Drive）：类别概率图（每类一个波段，波段顺序 = 类别编码升序，"
        "本项目编码 10/20/30/40/50 对应 甘蔗/水稻/桉树/其他作物/背景，值 0-100，"
        "可通过「类别名称」参数以「编码:名称」形式修改）+ 单波段分类图（类别编码），"
        "默认分辨率 10 m，坐标系统 WGS84（EPSG:4326），并附类别面积统计表。"
        "研究区大小不设上限：按 ≤2200×2200 瓦片流式下载（边界外瓦片自动跳过），"
        "大研究区同步计算耗时长（可能数小时），请保持页面不关闭，失败重跑会自动续传。"
        "GEE 凭证默认使用本机 earthengine authenticate 登录信息，"
        "部署到服务器时建议上传服务账号 JSON 凭证。"
    ),
    "params": [
        {"name": "feature_csv", "label": "第一步样本特征 CSV", "type": "csv", "required": True},
        {"name": "boundary", "label": "研究区矢量边界（shp 压缩包）", "type": "shp", "required": True},
        {"name": "label_property", "label": "标签属性列名", "type": "text", "required": True, "default": "class"},
        {"name": "year", "label": "分类年份", "type": "number", "required": True, "default": 2025},
        {"name": "cloud_pct", "label": "云量阈值（%）", "type": "number", "required": False, "default": 80},
        {"name": "scale", "label": "输出分辨率（米）", "type": "number", "required": False, "default": 10},
        {"name": "num_trees", "label": "随机森林树数量", "type": "number", "required": False, "default": 100},
        {
            "name": "class_names",
            "label": "类别名称（编码:名称 逗号分隔）",
            "type": "text",
            "required": False,
            "default": "10:甘蔗,20:水稻,30:桉树,40:其他作物,50:背景",
        },
        {"name": "max_train_points", "label": "训练样本上限（0=全部）", "type": "number", "required": False, "default": 0},
        {"name": "gee_project", "label": "GEE 项目 ID", "type": "text", "required": False, "default": DEFAULT_PROJECT},
        {"name": "gee_credentials", "label": "GEE 服务账号 JSON", "type": "json", "required": False},
    ],
}


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


def run(inputs: dict, workdir: Path) -> dict:
    """平台约定入口：解析参数 → 初始化 GEE → 训练 → 瓦片下载 → 合并 → 组装返回结构。"""
    workdir = Path(workdir)
    # 平台正常会创建输出目录，但直接调用（测试/复用）时可能不存在，这里兜底创建
    workdir.mkdir(parents=True, exist_ok=True)

    # ---------- 1. 参数解析（含默认值） ----------
    project = str(inputs.get("gee_project") or DEFAULT_PROJECT).strip()
    year = int(round(float(inputs.get("year") or DEFAULT_YEAR)))
    cloud_pct = float(inputs.get("cloud_pct") or DEFAULT_CLOUD_PCT)
    scale = float(inputs.get("scale") or DEFAULT_SCALE)
    num_trees = int(round(float(inputs.get("num_trees") or DEFAULT_NUM_TREES)))
    label_property = str(inputs.get("label_property") or "class").strip()
    class_names_param = str(inputs.get("class_names") or "").strip()
    max_train_points = int(round(float(inputs.get("max_train_points") or 0)))
    credentials_file = inputs.get("gee_credentials")

    if "feature_csv" not in inputs or "boundary" not in inputs:
        raise ValueError("必须提供「第一步样本特征 CSV」和「研究区矢量边界」。")
    feature_csv = inputs["feature_csv"]
    boundary_zip = inputs["boundary"]

    if not 2014 <= year <= 2030:
        raise ValueError(f"分类年份 {year} 超出支持范围（2014-2030）。")
    if not 0 <= cloud_pct <= 100:
        raise ValueError(f"云量阈值 {cloud_pct} 须在 0-100 之间。")
    if not 5 <= scale <= 100:
        raise ValueError(f"输出分辨率 {scale} 须在 5-100 米之间。")
    if not 10 <= num_trees <= 1000:
        raise ValueError(f"随机森林树数量 {num_trees} 须在 10-1000 之间。")
    if max_train_points < 0:
        raise ValueError("训练样本上限不能为负数。")

    # ---------- 2. GEE 初始化 ----------
    init_gee(project, credentials_file)

    # ---------- 3. 特征 CSV：波段识别 + 标签清洗 ----------
    df, lon_col, lat_col, coord_dropped = parse_sample_csv(Path(feature_csv), label_property)
    if coord_dropped:
        print(f"已跳过 {coord_dropped} 行坐标无效/越界的样本。")
    feature_bands, missing_bands = resolve_feature_bands(df.columns)
    if not feature_bands:
        raise ValueError(
            "样本特征 CSV 中未识别出任何特征波段列（期望「波段_月份」格式，如 B2_1、VH_12）。"
        )
    if missing_bands:
        print(
            f"  [提示] CSV 缺少 {len(missing_bands)} 个特征波段列（期望 133 个），"
            f"模型将只使用现有的 {len(feature_bands)} 个波段：{missing_bands[:8]}…"
        )

    label_vals = pd.to_numeric(df[label_property], errors="coerce")
    label_ok = label_vals.notna() & (label_vals == label_vals.round())
    train_df = df[label_ok].copy()
    train_df[label_property] = label_vals[label_ok].astype("int64")
    label_bad = int((~label_ok).sum())
    if train_df.empty:
        raise ValueError(f"标签列「{label_property}」没有有效的整数编码样本。")

    feature_bad = int(train_df[feature_bands].isna().any(axis=1).sum())
    train_df = train_df[~train_df[feature_bands].isna().any(axis=1)].reset_index(drop=True)
    if train_df.empty:
        raise ValueError("剔除特征含空值的样本后没有可用训练样本，请检查特征表数据完整性。")

    classes = sorted(train_df[label_property].unique().tolist())
    if len(classes) < 2:
        raise ValueError(f"训练集只有 {len(classes)} 个类别，随机森林至少需要 2 个类别。")
    if max(classes) > CLASS_NODATA - 5:
        raise ValueError(f"类别编码最大值 {max(classes)} 超出 Byte 分类图范围（须 ≤ {CLASS_NODATA - 5}）。")
    class_names = pair_class_names(classes, class_names_param)

    if max_train_points and len(train_df) > max_train_points:
        train_df = subsample_train(train_df, label_property, max_train_points)
        print(f"训练样本超过上限，已按类别比例分层抽样至 {len(train_df)} 个。")
    print(
        f"训练样本: {len(train_df)} 个（剔除标签无效 {label_bad}、特征含空值 {feature_bad}）；"
        f"类别: {dict(zip(classes, class_names))}"
    )

    # ---------- 4. 矢量边界与下载网格 ----------
    geom, bounds = read_boundary(Path(boundary_zip), workdir)
    pixel, nx, ny, tiles = plan_download_grid(bounds, scale, geom)
    n_download = sum(1 for t in tiles if t["download"])
    print(
        f"研究区网格: {nx:,}×{ny:,} 像元（{scale:.0f} m，EPSG:4326），"
        f"分 {len(tiles)} 个瓦片，其中 {n_download} 个需要下载。"
    )
    if nx * ny > HUGE_JOB_WARN_PX:
        print(
            f"  [提示] 研究区约 {nx * ny / 1e6:.0f} 百万像元（5 波段约 {nx * ny * 5 / 1e9:.1f} GB），"
            f"同步下载耗时可能达数小时；请保持后端运行与前端页面不关闭，"
            f"断点续传会在失败重跑时复用已下载的瓦片。"
        )

    # ---------- 5. 特征影像重建 + 随机森林训练 + 概率分类 ----------
    aoi = ee.Geometry.Rectangle([bounds[0], bounds[1], bounds[2], bounds[3]])
    final_image, _ = buildFeatureImage(year, aoi, cloud_pct)
    train_fc = build_training_features(train_df, lon_col, lat_col, label_property, feature_bands)
    prob_image = build_probability_image(
        final_image, feature_bands, train_fc, label_property, num_trees, len(classes)
    )

    # ---------- 6. 瓦片并行下载 ----------
    tile_dir = workdir / "tiles"
    tile_dir.mkdir(exist_ok=True)
    print(f"正在下载分类概率影像（{n_download} 个瓦片，{MAX_WORKERS} 线程并行）…")
    download_all_tiles(prob_image, tiles, pixel, len(classes), tile_dir)

    # ---------- 7. 合并瓦片 + 派生分类图 + 类别统计 ----------
    prob_tif, class_tif, class_counts = merge_tiles(
        workdir, tiles, pixel, nx, ny, bounds, geom, classes, class_names
    )
    if DELETE_TILES_AFTER_MERGE and tile_dir.is_dir():
        shutil.rmtree(tile_dir, ignore_errors=True)
        print("  瓦片缓存已删除（tiles/），仅保留合并后的结果文件。")
    stats_df, valid_px = build_class_stats(class_counts, classes, class_names, scale, bounds)
    stats_csv = workdir / "类别统计.csv"
    stats_df.to_csv(stats_csv, index=False, encoding="utf-8-sig")

    # ---------- 8. 分类图预览 PNG ----------
    png_path = workdir / "研究区分类图.png"
    try:
        render_class_preview(
            class_tif, png_path, class_names, classes, f"{year} 年研究区作物分类图（{scale:.0f} m）"
        )
    except Exception as exc:
        print(f"  [警告] 分类图预览生成失败（不影响结果）：{exc}")
        png_path = None

    # ---------- 9. 指标 ----------
    def push_metric(name, value, unit="", decimals=3):
        if isinstance(value, float) and not np.isfinite(value):
            return None
        return {"name": name, "value": float(value), "unit": unit, "decimals": decimals}

    metrics = [
        m for m in [
            push_metric("训练样本数", len(train_df), "个", 0),
            push_metric("特征波段数", len(feature_bands), "个", 0),
            push_metric("类别数", len(classes), "类", 0),
            push_metric("输出分辨率", scale, "m", 0),
            push_metric("研究区有效像元数", valid_px, "个", 0),
            push_metric("下载瓦片数", n_download, "个", 0),
            push_metric("剔除无效样本数", label_bad + feature_bad + coord_dropped, "个", 0),
        ] if m
    ]

    # ---------- 10. 表格 ----------
    train_dist = train_df[label_property].value_counts().sort_index().reset_index()
    train_dist.columns = ["编码", "样本数"]
    train_dist.insert(0, "类别", [dict(zip(classes, class_names)).get(int(c), str(c)) for c in train_dist["编码"]])
    train_csv = workdir / "训练集类别分布.csv"
    train_dist.to_csv(train_csv, index=False, encoding="utf-8-sig")

    tables = [
        {"name": "训练集类别分布", "columns": list(train_dist.columns), "rows": _json_rows(train_dist), "file": str(train_csv)},
        {"name": "类别统计", "columns": list(stats_df.columns), "rows": _json_rows(stats_df), "file": str(stats_csv)},
    ]

    # ---------- 11. 组装返回 ----------
    rasters = [
        {"name": "研究区分类概率图（0-100，波段：{}）".format("/".join(class_names)), "tif": str(prob_tif)},
        {"name": "研究区分类图（类别编码）", "tif": str(class_tif), **({"png": str(png_path)} if png_path else {})},
    ]
    top_cls = stats_df.iloc[0] if valid_px else None
    message = (
        f"{year} 年研究区随机森林分类完成：{len(train_df)} 个样本 × {len(feature_bands)} 个特征波段"
        f"训练 {num_trees} 棵树，{len(classes)} 个类别，输出 {scale:.0f} m 概率图与分类图（WGS84）。"
        + (f"占比最高类别：{top_cls['类别']}（{top_cls['占比(%)']}%）。" if top_cls is not None else "")
    )
    return {"message": message, "metrics": metrics, "tables": tables, "rasters": rasters}
