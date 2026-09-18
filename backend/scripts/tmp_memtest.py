# -*- coding: utf-8 -*-
"""临时实验：隔离 GEE User memory limit exceeded 的元凶。

候选因素：
  E0: 仅 1 月 11 波段 + 2000 点 —— 月度合成本身在此区域规模下的成本
  E2: 完整 133 波段(unmask) + 250 点 —— 小区域是否就能过
  E1: 完整 133 波段(unmask) + 500 点 —— 区域规模曲线
  E5: 完整 133 波段(去掉 unmask 年际填补) + 2000 点 —— unmask 图计算是否为元凶
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "algorithms"))

import ee
from future2 import (
    S1_SOURCE_BANDS, S2_SOURCE_BANDS, build_spatial_chunks,
    empty_named_image, features_from_dataframe, init_gee, maskS2clouds,
    minMaxDict, normalizeBand, parse_sample_csv,
)

init_gee("test1-506208", None)
df, lon_col, lat_col, _ = parse_sample_csv(Path("data/table.csv"), "class")
feats = features_from_dataframe(df, lon_col, lat_col)
fc = ee.FeatureCollection(feats)
aoi = fc.geometry()

print("构建月度合成集合…", flush=True)
months = ee.List.sequence(1, 12)


def process_month(m):
    m = ee.Number(m)
    start = ee.Date.fromYMD(2025, m, 1)
    end = start.advance(1, "month")
    s2c = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
           .filterBounds(aoi).filterDate(start, end)
           .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 80))
           .map(maskS2clouds))
    s2 = ee.Image(ee.Algorithms.If(s2c.size().gt(0), s2c.median(), empty_named_image(S2_SOURCE_BANDS)))
    s1c = (ee.ImageCollection("COPERNICUS/S1_GRD")
           .filterBounds(aoi).filterDate(start, end)
           .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
           .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
           .filter(ee.Filter.eq("instrumentMode", "IW"))
           .select(["VV", "VH"]))
    s1 = ee.Image(ee.Algorithms.If(s1c.size().gt(0), s1c.median(), empty_named_image(S1_SOURCE_BANDS)))
    evi = s2.expression(
        "2.5*((B8-B4)/(B8+6*B4-7.5*B2+1))",
        {"B8": s2.select("B8"), "B4": s2.select("B4"), "B2": s2.select("B2")},
    ).rename("EVI")
    combined = (s2.select(["B2", "B3", "B4", "B8", "B11", "B12"])
                .addBands(s2.normalizedDifference(["B8", "B4"]).rename("NDVI"))
                .addBands(evi)
                .addBands(s2.normalizedDifference(["B3", "B8"]).rename("NDWI"))
                .addBands(s1))
    return ee.Image.cat(
        [normalizeBand(combined, n, mn, mx) for n, (mn, mx) in minMaxDict.items()]
    ).set("month", m)


monthly = months.map(process_month)
month_col = ee.ImageCollection.fromImages(monthly)
annual_median = month_col.median()
img_m1 = ee.Image(monthly.get(0))


def fill(img, do_unmask):
    img = ee.Image(img)
    m_str = ee.Number(img.get("month")).format("%d")

    def rn(b):
        return ee.String(b).cat("_").cat(m_str)

    renamed = img.rename(img.bandNames().map(rn))
    if do_unmask:
        return renamed.unmask(annual_median)
    return renamed


def strip_prefix(stacked):
    return stacked.rename(
        stacked.bandNames().map(lambda n: ee.String(n).replace("^[0-9]+_", "", "r"))
    )


stack_unmasked = strip_prefix(month_col.map(lambda i: fill(i, True)).toBands())
stack_masked = strip_prefix(month_col.map(lambda i: fill(i, False)).toBands())

chunks = build_spatial_chunks(df, lon_col, lat_col)
c1 = chunks[0]
sub = df[df["point_id"].isin(c1)].sort_values(lon_col)
id_all = c1
id_500 = sub["point_id"].iloc[:500].tolist()
id_250 = sub["point_id"].iloc[:250].tolist()


def try_run(tag, image, ids, ts=4):
    try:
        out = image.sampleRegions(
            collection=fc.filter(ee.Filter.inList("point_id", ee.List(ids))),
            properties=["class", "point_id"],
            scale=10, tileScale=ts, geometries=False,
        ).getInfo()
        print(f"{tag}: 成功，{len(out['features'])} 点", flush=True)
    except Exception as exc:
        print(f"{tag}: 失败 -> {str(exc)[:90]}", flush=True)


try_run("E0 仅1月11波段 2000点", img_m1, id_all)
try_run("E2 133波段(unmask) 250点", stack_unmasked, id_250)
try_run("E1 133波段(unmask) 500点", stack_unmasked, id_500)
try_run("E5 133波段(无unmask) 2000点", stack_masked, id_all)
print("实验完成", flush=True)
