"""Fixed parameters for the "GEE crop classification" algorithm.

These constants are part of the algorithm specification and must NOT be
exposed to the user as editable form fields (with the exception of
``num_trees``, ``train_ratio`` and ``label_property`` which are surfaced
under the collapsed "其他参数" section for advanced users only). They
live here as a single source of truth so future maintainers only need to
touch one place.

Exposed to the front-end as readonly values for display only
(see `crop_classification.adapter.CropClassificationAdapter.params_schema`).
"""
from __future__ import annotations

from typing import List


# --- Sentinel-2/1 band stack: same order used during normalization / rename.
# Mirrors ``app.algorithms.configs.time_series_config.BAND_NAMES`` so the
# classification input feature space stays consistent with the upstream
# feature-extraction algorithm. Values are duplicated here (instead of
# importing from time_series_config) to keep algorithm modules decoupled.
BAND_NAMES: List[str] = [
    "B2",
    "B3",
    "B4",
    "B8",
    "B11",
    "B12",
    "NDVI",
    "EVI",
    "NDWI",
    "VV",
    "VH",
]


# Per-band minimum values (from the original standalone script; values are
# kept verbatim so the classification output is byte-for-byte reproducible
# against the prior hard-coded run).
MIN_VALUES: List[float] = [
    0.026847062426510727,
    0.04344944377607401,
    0.02839820780491779,
    0.02049033818835525,
    0.016678254923141185,
    0.014652389678614614,
    -0.2697917553317518,
    -0.05309414680605596,
    -0.738070698375942,
    -19.57493155721727,
    -33.13496576376786,
]


# Per-band maximum values (from the original standalone script; values are
# kept verbatim so the classification output is byte-for-byte reproducible
# against the prior hard-coded run).
MAX_VALUES: List[float] = [
    0.09232069038111589,
    0.11377479614148757,
    0.11808952841035082,
    0.37190000022329933,
    0.27036065498336415,
    0.18863233387470246,
    0.8241633234169772,
    0.6229241342439411,
    0.5195067842930323,
    -3.1854212111818003,
    -10.131209855449935,
]


# DEM normalization bounds (NASA/NASADEM_HGT/001 elevation in meters).
# Verbatim from the original standalone script.
DEM_MIN: float = -11.0
DEM_MAX: float = 1749.0


# Sentinel-2 cloud cover filter (was hard-coded to 80 in the original script).
CLOUD_PCT_MAX: int = 80


# RandomForest tree count (was hard-coded to 100 in the original script).
NUM_TREES: int = 100


# Train/validation split (was hard-coded to 0.8 in the original script).
TRAIN_RATIO: float = 0.8


# Property name on each sample Feature that holds the integer class label.
LABEL_PROPERTY: str = "class"


# Nodata sentinel for the final stacked image (matches the original script's
# ``stackedTimeSeries.unmask(-9999)``).
STACK_NODATA: float = -9999.0


# Default prefix used for the GEE Export.image.toAsset description when the
# user does not provide a custom ``task_name``. A UTC timestamp is appended
# in ``run()`` to avoid collisions across re-submissions.
DEFAULT_TASK_PREFIX: str = "CROPClassification"


# Per-month per-band feature count: 11 bands × 12 months = 132, +1 DEM = 133.
FEATURE_BAND_COUNT: int = len(BAND_NAMES)            # 11
TIME_SERIES_MONTHS: int = 12                         # January through December
TOTAL_FEATURE_COUNT: int = (
    FEATURE_BAND_COUNT * TIME_SERIES_MONTHS + 1     # 133
)


# Output band naming prefix for each month's stack.
MONTH_BAND_PREFIX: str = "M"                         # produces M01_B2, M02_VH, ...
DEM_BAND_NAME: str = "elevation"


# GEE export defaults.
EXPORT_SCALE: int = 10
EXPORT_CRS: str = "EPSG:4326"
EXPORT_MAX_PIXELS: int = int(1e13)


# Class label / colour mapping for the categorical preview rendering.
#
# Values come from the original standalone script's hard-coded reclassify
# lookup:
#   10 -> 甘蔗 (sugarcane)
#   20 -> 水稻 (rice)
#   30 -> 桉树 (eucalyptus)
#   40 -> 其他作物 (other crops)
#   50 -> 背景 (background)
#
# The integer keys are the unique pixel values present in the output
# classification raster; the labels are shown verbatim on the frontend
# preview legend instead of "类别 10 / 类别 20 ...".
CLASS_LABELS: dict = {
    10: "甘蔗",
    20: "水稻",
    30: "桉树",
    40: "其他作物",
    50: "背景",
}

CLASS_COLORS: dict = {
    10: "#16a34a",  # green  - sugarcane
    20: "#eab308",  # amber  - rice
    30: "#15803d",  # darker green - eucalyptus
    40: "#f97316",  # orange - other crops
    50: "#94a3b8",  # slate  - background
}


# Suffix for the per-file class metadata sidecar. The classification
# finalize() drops a JSON file named "<output_tif>.metadata.json" next to the
# downloaded classification GeoTIFF; preview_service.load_class_metadata() reads
# it and exposes class labels / colours on the frontend preview legend.
CLASS_METADATA_SUFFIX: str = ".metadata.json"


__all__ = [
    "BAND_NAMES",
    "MIN_VALUES",
    "MAX_VALUES",
    "DEM_MIN",
    "DEM_MAX",
    "CLOUD_PCT_MAX",
    "NUM_TREES",
    "TRAIN_RATIO",
    "LABEL_PROPERTY",
    "STACK_NODATA",
    "DEFAULT_TASK_PREFIX",
    "FEATURE_BAND_COUNT",
    "TIME_SERIES_MONTHS",
    "TOTAL_FEATURE_COUNT",
    "MONTH_BAND_PREFIX",
    "DEM_BAND_NAME",
    "EXPORT_SCALE",
    "EXPORT_CRS",
    "EXPORT_MAX_PIXELS",
    "CLASS_LABELS",
    "CLASS_COLORS",
    "CLASS_METADATA_SUFFIX",
]