"""Fixed parameters for the "crop time-series features" algorithm.

These constants are part of the algorithm specification and must NOT be
exposed to the user as editable form fields. They live here as a single
source of truth so future maintainers only need to touch one place.

Exposed to the front-end as readonly values for display only
(see `crop_features.adapter.CropFeaturesAdapter.params_schema`).
"""
from __future__ import annotations

from typing import List


# --- Sentinel-2/1 band stack: same order used during normalization / rename.
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

# Per-band minimum values (from user's original script).
# Order matches BAND_NAMES.
MIN_VALUES: List[float] = [
    0.026847,
    0.043449,
    0.028398,
    0.020490,
    0.016678,
    0.014652,
    -0.269792,
    -0.053094,
    -0.738071,
    -19.5749,
    -33.1350,
]

# Per-band maximum values (from user's original script).
# Order matches BAND_NAMES.
MAX_VALUES: List[float] = [
    0.092321,
    0.113775,
    0.118090,
    0.371900,
    0.270361,
    0.188632,
    0.824163,
    0.622924,
    0.519507,
    -3.1854,
    -10.1312,
]

# DEM normalization bounds (NASA/NASADEM_HGT/001 elevation in meters).
DEM_MIN: float = -44.0
DEM_MAX: float = 2808.0

# Sentinel-2 cloud cover filter (was hard-coded to 80 in user's script).
CLOUD_PCT_MAX: int = 80

# Per-month per-band feature count: 11 bands × 12 months = 132, +1 DEM = 133.
FEATURE_BAND_COUNT: int = len(BAND_NAMES)            # 11
TIME_SERIES_MONTHS: int = 12                         # January through December
TOTAL_FEATURE_COUNT: int = (
    FEATURE_BAND_COUNT * TIME_SERIES_MONTHS + 1     # 133
)

# Output band naming prefix for each month's stack.
MONTH_BAND_PREFIX: str = "M"                         # produces M01_B2, M02_VH, ...
DEM_BAND_NAME: str = "elevation"

# Nodata sentinel for unmasked final stack (matches user's original script).
STACK_NODATA: float = -9999.0

# GEE Export.table.toDrive defaults.
GEE_FOLDER: str = "GEE_Exports"
GEE_FILE_FORMAT: str = "CSV"


__all__ = [
    "BAND_NAMES",
    "MIN_VALUES",
    "MAX_VALUES",
    "DEM_MIN",
    "DEM_MAX",
    "CLOUD_PCT_MAX",
    "FEATURE_BAND_COUNT",
    "TIME_SERIES_MONTHS",
    "TOTAL_FEATURE_COUNT",
    "MONTH_BAND_PREFIX",
    "DEM_BAND_NAME",
    "STACK_NODATA",
    "GEE_FOLDER",
    "GEE_FILE_FORMAT",
]