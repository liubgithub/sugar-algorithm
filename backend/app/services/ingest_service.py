"""Local file → GEE asset ingestion.

`crop_threshold` (and any other GEE algorithm that takes a "research area"
shape) historically required users to manually upload their shp/geojson
to GEE and paste the resulting asset id. This module lets the algorithm
accept a *local* file path and auto-ingest it to a GEE asset on the fly.

Public surface
--------------
- `ensure_asset_for_local(...)`  – idempotent: reuses a previously
  registered asset if the file_id is already cached, otherwise registers
  one. Returns a `projects/<project>/assets/<name>` string ready to feed
  into `ee.FeatureCollection(asset_id)` / `ee.Image(asset_id)`.

Design
------
- Cache lives at `data/algorithm_data/<algo>/<slot>/_gee_assets.json`,
  keyed by `file_id`. The file_id is extracted from the upload-on-disk
  path convention `<file_id>__<original_name>` produced by
  `app.api.uploads._atomic_target`.
- `kind_hint='feature_collection' | 'image' | None` lets the caller
  force a specific GEE asset type. When None, we infer from extension:
    .geojson / .json  -> TABLE  (GEE FeatureCollection, via geemap inline)
    .csv              -> TABLE  (CSV → GeoJSON FeatureCollection dict via
                                stdlib `csv.DictReader`, then
                                `ee.FeatureCollection(geojson_dict)`; no
                                pandas / geemap / PyPI `geojson` needed.
                                Expects `longitude` / `latitude` columns.)
    .shp              -> TABLE  (shp → ee.FeatureCollection via geemap,
                                then ee.batch.Export.table.toAsset)
    .tif / .tiff      -> IMAGE  (NOT supported in-process; we raise a
                                clear error telling the user to upload
                                manually via the GEE Code Editor and
                                paste the asset id)
- Auth: the calling algorithm is expected to have already run its own
  `_initialize_ee(project)` before calling this module; we don't
  re-initialize EE here to avoid double-initializing from threads.

Large files (>9 MB serialized FC) — chunked upload
-------------------------------------------------
``ee.batch.Export.table.toAsset`` ships the FeatureCollection inside
the request body. GEE hard-caps that body at 10 MB and returns
``Request payload size exceeds the limit: 10485760 bytes.`` — which
the calling algorithm surfaces as "特征文件上传到 GEE 失败". A user CSV
from ``crop_features`` carries ~133 numeric columns per row, so the
limit trips around 5–7 k samples — well below what the user would
consider "large". We can't use ``ee.data.startIngestion`` because it
needs a hosted URL (GCS / HTTP), and we don't have a GCS bucket.

Workaround for CSV / GeoJSON sources: split the FC dict client-side
into chunks whose serialized JSON is ≤ 9 MB, upload each to a throw-
away temp asset, wait for all of them to land, then merge them
server-side via ``ee.FeatureCollection([c1, c2, …]).flatten()`` and
re-export to the final asset id. The merge manifest only references
asset ids, so it never hits the 10 MB request cap. Intermediate
chunk assets are deleted best-effort afterwards to free storage.
Shp path keeps the old single-upload behaviour (shp-to-ee returns an
ee.FC we can't cheaply measure without a server roundtrip; research
areas are typically small).

Why geemap (not earthengine-api directly)
-----------------------------------------
`ee.data.startIngestion(request_id, params, allow_overwrite=False)`
expects ``params['sources']`` to contain ``gs://`` URIs to Google Cloud
Storage — i.e. the source files must already live in a GCS bucket the
caller controls. There is no built-in path in earthengine-api to push a
local file to GEE without an external GCS bucket.

The community-maintained `geemap` library wraps earthengine-api and
offers `shp_to_ee()` / `geojson_to_ee()` which build an
``ee.FeatureCollection`` directly from local files (no GCS needed) and
`ee_export_vector_to_asset()` which creates the GEE batch task that
promotes it to a real asset. We delegate to those helpers here.

Dependencies
------------
- ``earthengine-api`` (>= 1.5) for ``ee.batch.Export.table.toAsset``.
- ``geemap`` (>= 0.30) for ``shp_to_ee`` / ``geojson_to_ee``.
- ``pycrs`` — pulled in by geemap for `.prj` CRS parsing. Without it
  ``shp_to_ee`` raises ``ModuleNotFoundError: No module named 'pycrs'``
  the moment a shapefile with sidecar `.prj` is ingested. All three
  are pinned in `requirements.txt`; do not drop ``pycrs`` from a fresh
  environment or ingest of real-world shapefiles will fail.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, List, Optional, Tuple

from app.core.config import ALGO_DATA_DIR


# ---- Cache IO --------------------------------------------------------------

CACHE_FILENAME = "_gee_assets.json"

# Module version banner — printed on every ingest call so a freshly
# restarted server is easy to verify in the logs. Bump when the upload
# pipeline changes behaviour.
INGEST_SERVICE_VERSION = "2026-09-26-multichunk"


def _cache_path(algorithm: str, slot: str) -> Path:
    p = ALGO_DATA_DIR / algorithm / slot / CACHE_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_cache(algorithm: str, slot: str) -> dict:
    """Load the cache file, returning an empty dict on any structural error.

    Defensive against:
      - missing / unreadable file (e.g. concurrent truncate)
      - invalid JSON (e.g. partially-written by another process)
      - top-level non-object (an old or hand-edited file might be a list,
        a string, or null)
      - values that aren't dicts (an old version of this module or a
        manual edit might have stored plain strings or asset_ids directly
        as the value). Callers used to assume
        ``cache[file_id].get("asset_id")``, which raises
        ``'str' object has no attribute 'items'`` on those legacy entries.
    We strip non-dict values out on load so callers can keep their
    existing ``cached.get(...)`` calls.
    """
    p = _cache_path(algorithm, slot)
    if not p.is_file():
        return {}
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    cleaned: dict = {}
    for k, v in data.items():
        if isinstance(v, dict):
            cleaned[k] = v
        else:
            # Legacy / malformed entry — drop it so the caller falls back
            # to a fresh ingest rather than crashing on .get(...).
            print(f"[ingest] 忽略 cache 中非 dict 条目：{k!r} -> {type(v).__name__}")
    return cleaned


def _save_cache(algorithm: str, slot: str, data: dict) -> None:
    """Atomically write `data` to the cache file.

    Writes to a sibling `.tmp` first and renames over the target. Avoids
    the "another process read a half-written JSON" race that can leave
    a corrupted file on disk (and the next ``_load_cache`` would then
    return ``{}`` silently — losing the cache — or worse, load a partial
    dict that mismatches expectations downstream).
    """
    p = _cache_path(algorithm, slot)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            # fsync may be unsupported on some Windows filesystems; the
            # rename below still gives us atomicity within one FS.
            pass
    os.replace(tmp, p)


# ---- file_id derivation ---------------------------------------------------

# Uploaded files use the convention "<file_id>__<safe_name>" produced by
# app/api/uploads.py::_atomic_target. Pull out the file_id prefix.
_FILE_ID_RE = re.compile(r"^(?P<fid>[A-Za-z0-9_\-]{6,32})__")


def _derive_file_id(local_path: Path, algorithm: str, slot: str) -> str:
    """Best-effort file_id from the path.

    Walks up the directory chain until we find a name matching the upload
    convention. Falls back to a stable hash of the path so re-using the
    same local file still hits the cache.
    """
    parts = local_path.parts
    for part in parts:
        m = _FILE_ID_RE.match(part)
        if m:
            return m.group("fid")
    # Last-resort: derive a stable id from the path itself so the cache
    # is still useful across runs.
    import hashlib

    digest = hashlib.sha1(str(local_path.resolve()).encode("utf-8")).hexdigest()[:12]
    return f"local_{digest}"


# ---- Kind inference -------------------------------------------------------

_TIF_EXTS = {".tif", ".tiff"}
_VEC_EXTS = {".geojson", ".json", ".csv", ".shp", ".zip"}


def _infer_kind(local_path: Path, kind_hint: Optional[str]) -> str:
    """Return 'feature_collection' or 'image'."""
    if kind_hint in ("feature_collection", "image"):
        return kind_hint
    suffix = local_path.suffix.lower()
    if suffix in _TIF_EXTS:
        return "image"
    # geojson / json / shp / zip (with shp inside) -> vector
    return "feature_collection"


# ---- Asset name synthesis -------------------------------------------------


def _make_asset_name(algorithm: str, slot: str, project: str) -> str:
    """Build a unique `projects/<project>/assets/<algo>_<slot>_<8hex>` asset name."""
    # asset name must be lowercase letters / digits / underscores.
    base = f"{algorithm}_{slot}_{uuid.uuid4().hex[:8]}".lower()
    base = re.sub(r"[^a-z0-9_]", "_", base)
    return f"projects/{project}/assets/{base}"


# ---- Vector source extraction --------------------------------------------


def _flatten_zip_to_shp_dir(zip_path: Path, work: Path) -> Path:
    """Extract a .zip and guarantee a .shp sits at the work/ root.

    Handles the common "user zipped a folder/" layout by flattening
    when there is exactly one top-level directory and it contains a
    .shp. Returns the directory to use as the vector source.
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(work)

    entries = [p for p in work.iterdir() if not p.name.startswith("__MACOSX")]
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        if any(p.suffix.lower() == ".shp" for p in inner.iterdir()):
            for child in inner.iterdir():
                shutil.move(str(child), str(work / child.name))
            shutil.rmtree(inner, ignore_errors=True)
    if not any(p.suffix.lower() == ".shp" for p in work.iterdir()):
        raise ValueError(
            f"研究区 zip 中未找到 .shp 文件，请确保压缩包内含 .shp/.dbf/.shx：{zip_path}"
        )
    return work


def _resolve_coord_columns(
    headers: List[str], wants: Tuple[str, ...]
) -> Tuple[Dict[str, str], List[str]]:
    """Map each desired key (e.g. ``'longitude'``) to the actual CSV
    header that represents it.

    Matching is case-insensitive and tolerates leading/trailing spaces
    around header names. Preference is given to:
      1. exact match (case-sensitive, with whitespace stripped),
      2. case-insensitive match against any of ``wants``.

    Returns ``(found_map, missing_keys)``. ``found_map`` has the
    desired key as the dict key and the matched CSV header as the
    value. ``missing_keys`` lists the desired keys that did not appear
    in the header (so the caller can format a precise error message).
    """
    norm_headers = [(h or "").strip() for h in headers]
    # Map each stripped header to the original (unstripped) name so we can
    # echo it back in error messages exactly as the user wrote it.
    norm_to_orig = {n: o for n, o in zip(norm_headers, headers)}
    found: Dict[str, str] = {}
    missing: List[str] = []
    for want in wants:
        # 1) exact (post-ws-strip) match
        if want in norm_to_orig:
            found[want] = norm_to_orig[want]
            continue
        # 2) case-insensitive match
        ci = want.casefold()
        for n in norm_headers:
            if n.casefold() == ci:
                found[want] = norm_to_orig[n]
                break
        else:
            missing.append(want)
    # Return stripped names so callers can use them directly as dict
    # keys (the per-row loop strips header whitespace when building
    # the lookup dict).
    found_stripped = {k: v.strip() for k, v in found.items()}
    return found_stripped, missing


def _csv_to_geojson_feature_collection(
    local_path: Path,
    *,
    longitude: str = "longitude",
    latitude: str = "latitude",
) -> dict:
    """Convert a CSV file to a GeoJSON FeatureCollection of Point features.

    Uses stdlib `csv.DictReader` only — no pandas, no geemap, no PyPI
    `geojson` package. The first row is treated as the header; missing
    columns or non-numeric lon/lat values raise ``ValueError`` with a
    precise, user-facing message.

    Coordinate column matching is **case-insensitive**: ``longitude``,
    ``Longitude``, ``LONGITUDE`` are all accepted. Header names are also
    trimmed of surrounding whitespace.

    Parameters
    ----------
    local_path : Path
        Path to the CSV file (must be UTF-8 decodable).
    longitude, latitude : str
        Names of the columns holding the coordinates. Matched
        case-insensitively against the CSV header (whitespace ignored).

    Returns
    -------
    dict
        A GeoJSON FeatureCollection dict suitable for
        ``ee.FeatureCollection(...)``. Each row becomes a Feature whose
        ``properties`` map contains every CSV column other than the
        coordinate columns.
    """
    import csv as _csv

    with local_path.open("r", encoding="utf-8", newline="") as fh:
        # sniff a small sample to detect delimiter (commas vs tabs vs
        # semicolons); fall back to the excel dialect which auto-guesses.
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = _csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except _csv.Error:
            dialect = _csv.excel
        reader = _csv.DictReader(fh, dialect=dialect)
        headers = reader.fieldnames or []
        rows = list(reader)

    if not rows:
        raise ValueError(
            "CSV 内容为空，请至少准备 1 行样本点数据；"
            "首行为表头，必须包含 longitude 与 latitude 两列"
            "（大小写不限，例如 longitude / Longitude / LONGITUDE）。"
        )

    found, missing = _resolve_coord_columns(headers, (longitude, latitude))
    if missing:
        # 在错误信息中告知用户可接受的大小写写法。
        hint = (
            "请确认首行为英文表头，并包含两个坐标列（大小写不限，"
            "如 longitude / Longitude / LONGITUDE 与 latitude / Latitude / LATITUDE）。"
        )
        raise ValueError(
            f"CSV 缺少必需列：{', '.join(missing)}。"
            f"当前列名：{', '.join(headers) or '(空)'}。"
            f"{hint}"
        )

    lon_col = found[longitude]
    lat_col = found[latitude]

    features: list = []
    for i, row in enumerate(rows, start=2):  # start=2: 第 1 行是表头
        # Strip whitespace around cell values — common when CSV is
        # exported from Excel with leading/trailing spaces.
        cleaned = {
            (k or "").strip(): ("" if v is None else str(v).strip())
            for k, v in row.items()
        }
        try:
            lon_f = float(cleaned[lon_col])
            lat_f = float(cleaned[lat_col])
        except (KeyError, ValueError) as exc:
            raise ValueError(
                f"CSV 第 {i} 行 {lon_col}/{lat_col} 无法转为数字：{exc}"
                f"（{lon_col}='{cleaned.get(lon_col, '')}', "
                f"{lat_col}='{cleaned.get(lat_col, '')}'）。"
            ) from exc
        properties = {
            k: v for k, v in cleaned.items() if k not in (lon_col, lat_col)
        }
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon_f, lat_f]},
            "properties": properties,
        })

    return {"type": "FeatureCollection", "features": features}


def _locate_shp(local_path: Path) -> Path:
    """Return the path of the .shp to feed to geemap.

    Accepts:
      - a single .shp file
      - a .zip containing a .shp
      - a directory that contains a .shp at any depth (e.g. after
        upload extraction via webkitdirectory where the browser-added
        top-level dir name lives inside our slot dir)
    """
    suffix = local_path.suffix.lower()
    if suffix == ".shp":
        return local_path
    if suffix == ".zip":
        with tempfile.TemporaryDirectory(prefix="gee_unzip_") as tmp:
            tmp_dir = Path(tmp)
            return _flatten_zip_to_shp_dir(local_path, tmp_dir)
    if local_path.is_dir():
        # Look in the directory itself first, then one level deep. The
        # one-level-deep case matches uploads.py's webkitdirectory layout
        # where the browser wraps user files inside `<dirname>/...`.
        candidates = list(local_path.glob("*.shp")) + list(local_path.glob("*/*.shp"))
        # De-dup and prefer the shallowest match.
        seen: set = set()
        ordered: list = []
        for c in candidates:
            try:
                rel = c.relative_to(local_path)
            except ValueError:
                continue
            depth = len(rel.parts)
            key = (depth, str(c).lower())
            if key in seen:
                continue
            seen.add(key)
            ordered.append(c)
        ordered.sort(key=lambda p: (len(p.relative_to(local_path).parts), p.name.lower()))
        if not ordered:
            raise ValueError(f"研究区目录中未找到 .shp 文件：{local_path}")
        if len(ordered) > 1:
            print(
                f"[ingest] 目录中存在多个 .shp，使用第一个：{ordered[0].name}"
            )
        return ordered[0]
    raise ValueError(f"暂不支持的矢量文件类型：{local_path}")


# ---- GEE ingestion --------------------------------------------------------

# GEE's ``Export.table.toAsset`` request body has a hard 10 MB cap. We work
# 1 MB below that to leave room for request envelope / headers / query
# metadata that earthengine-api injects on top of the FeatureCollection JSON.
_MAX_GEE_UPLOAD_BYTES = int(9 * 1024 * 1024)


def _estimate_geojson_bytes(geojson_dict: dict) -> int:
    """Return UTF-8 byte size of a GeoJSON dict after ``json.dumps``."""
    return len(json.dumps(geojson_dict, ensure_ascii=False).encode("utf-8"))


def _split_geojson_features(
    geojson_dict: dict, *, max_bytes: int = _MAX_GEE_UPLOAD_BYTES
) -> List[dict]:
    """Split a FeatureCollection dict into chunks each ≤ ``max_bytes`` serialized.

    O(N) algorithm — precomputes per-feature JSON byte size once, then
    greedily accumulates features into chunks whose envelope fits under
    the limit. The previous incremental approach was O(N²) because it
    re-serialized the growing ``current`` buffer on every boundary check,
    which becomes prohibitive (~hours) for 30 MB+ CSVs (~15 k+ rows).

    Raises ``ValueError`` if a single feature alone exceeds ``max_bytes`` —
    chunking can't help in that case and the caller should surface a
    precise error message to the user.
    """
    features = list(geojson_dict.get("features") or [])
    if not features:
        return [dict(geojson_dict)]

    # Per-feature JSON size — each feature serialized exactly once. On
    # a 30 MB CSV (~18 k wide rows × ~2 KB / row) this is ~18 k json.dumps
    # calls = a few seconds. The previous version's incremental re-serialize
    # of a growing buffer turned this into O(N²), i.e. many minutes.
    feature_sizes = [
        len(json.dumps(f, ensure_ascii=False).encode("utf-8"))
        for f in features
    ]

    # JSON envelope budget per chunk:
    # - 4 KB for ``{"type":"FeatureCollection","features":[ … ]}``
    # - 2 bytes for ``, `` between consecutive features
    WRAPPER_ENVELOPE = 4096
    SEPARATOR = 2

    chunks: List[dict] = []
    current: list = []
    current_size: int = 0  # sum of per-feature JSON sizes, no separators yet
    for feat, fsize in zip(features, feature_sizes):
        separator = SEPARATOR if current else 0
        if current and current_size + separator + fsize + WRAPPER_ENVELOPE > max_bytes:
            chunks.append({"type": "FeatureCollection", "features": current})
            current = [feat]
            current_size = fsize
        else:
            current.append(feat)
            current_size += fsize + separator
    if current:
        chunks.append({"type": "FeatureCollection", "features": current})
    return chunks





def _ingest_vector_via_geemap(
    *,
    asset_id: str,
    local_path: Path,
    kind: str,
) -> List[str]:
    """Build an ee.FeatureCollection from a local vector file and export
    it to GEE asset(s) via ``ee.batch.Export.table.toAsset``.

    Returns
    -------
    list[str]
        A list of GEE asset ids. Single-element list ``[asset_id]`` for
        small files / SHP. Multi-element list of ``<asset_id>_chunkNNN``
        ids for large CSV / GeoJSON that had to be chunked. Callers
        reconstruct the full FC server-side via
        ``ee.FeatureCollection([ids]).flatten()`` — see ``ensure_asset_for_local``.

    Large files (>9 MB serialized FC)
    ---------------------------------
    GEE's ``Export.table.toAsset`` request body has a hard 10 MB cap and
    we cannot work around it server-side: ``Export.table.toAsset`` with
    a flattened FC of existing assets still triggers a ``getInfo()`` that
    re-fetches the entire merged payload, blowing the same 10 MB limit.
    So we do *not* merge chunks back into a single asset. Instead each
    chunk becomes its own asset (``<asset_id>_chunk000`` ...) and the
    caller concatenates them at use time via ``.flatten()``.

    For CSV / GeoJSON the assembled FC is measured before upload; if it
    exceeds ``_MAX_GEE_UPLOAD_BYTES`` (9 MB) we split it into smaller
    chunks (O(N) algorithm via precomputed per-feature byte sizes) and
    upload each as a standalone asset.

    For SHP ``geemap.shp_to_ee`` returns an ``ee.FC`` we can't cheaply
    measure without a server roundtrip; research areas are typically
    small enough that we keep the single-upload behaviour.
    """
    # Module banner — visible in server logs to confirm this version is
    # actually loaded after a restart (helps diagnose "I edited the file
    # but still see the old error" reports).
    print(f"[ingest] _ingest_vector_via_geemap v={INGEST_SERVICE_VERSION}")

    import ee

    try:
        import geemap  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "自动注册研究区需要 geemap 库（pip install geemap）。"
            " 或者在 GEE Code Editor 上传后，把 Asset 路径填到『研究区 GEE Asset 路径』字段。"
        ) from exc

    if kind == "image":
        # geemap does not provide a "tif → ee.Image" + toAsset helper
        # without a GCS bucket. We refuse cleanly and ask the user to
        # upload manually.
        raise RuntimeError(
            "栅格研究区（.tif）当前需要您手动上传："
            "在 GEE Code Editor 的 Assets 标签页 → New → Image Upload → "
            "上传后把 Asset 路径填到『研究区 GEE Asset 路径』字段。"
        )

    description = f"ingest_{Path(local_path).stem}"
    fc: Optional[Any] = None  # ee.FeatureCollection when built directly
    geojson_dict: Optional[dict] = None  # kept so we can measure + chunk

    # ----- Build the source (FC for shp; GeoJSON dict for csv/geojson) -----
    if local_path.suffix.lower() in {".geojson", ".json"}:
        try:
            geojson_dict = json.loads(local_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"解析 GeoJSON 失败：{exc}") from exc
        if (
            not isinstance(geojson_dict, dict)
            or geojson_dict.get("type") != "FeatureCollection"
        ):
            raise RuntimeError(
                f"仅支持 FeatureCollection 类型的 GeoJSON 文件，"
                f"当前类型：{geojson_dict.get('type') if isinstance(geojson_dict, dict) else type(geojson_dict).__name__}。"
                " 单条 Feature 请先包装为 FeatureCollection 再上传。"
            )
    elif local_path.suffix.lower() == ".csv":
        # CSV → ee.FeatureCollection. We intentionally don't go through
        # ``geemap.csv_to_ee``: that helper transitively imports the PyPI
        # ``geojson`` package, which is not in this project's dependency
        # set (and would be the second call of ``geemap`` on the CSV path
        # when we can do the conversion with stdlib alone).
        try:
            geojson_dict = _csv_to_geojson_feature_collection(local_path)
        except ValueError:
            # Already a user-facing message; pass through verbatim.
            raise
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"CSV 解析失败：{exc}") from exc
    else:
        # .shp (or zip / dir containing .shp) — geemap returns an ee.FC.
        shp_path = _locate_shp(local_path)
        try:
            fc = geemap.shp_to_ee(str(shp_path))
        except Exception as exc:
            raise RuntimeError(
                f"shp 解析失败：{exc}。"
                " 请确认 CRS 为 EPSG:4326，或在 GEE Code Editor 上传后填 Asset 路径。"
            ) from exc

    # ----- Single upload vs. chunked upload -----
    if geojson_dict is not None:
        features = geojson_dict.get("features") or []
        if not features:
            raise RuntimeError(
                f"FeatureCollection 中没有任何要素：{local_path}。"
                " 请检查 CSV / GeoJSON 是否至少包含 1 行 / 1 个要素。"
            )
        payload_bytes = _estimate_geojson_bytes(geojson_dict)
        if payload_bytes <= _MAX_GEE_UPLOAD_BYTES:
            # Fast path: under the limit, single upload.
            print(
                f"[ingest] 单次上传 {asset_id}："
                f"{len(features)} features, {payload_bytes / 1024 / 1024:.2f} MB"
            )
            try:
                fc = ee.FeatureCollection(geojson_dict)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"构造 ee.FeatureCollection 失败：{exc}") from exc
            task = ee.batch.Export.table.toAsset(
                collection=fc,
                description=description,
                assetId=asset_id,
            )
            task.start()
            return [asset_id]
        else:
            # Slow path: split into chunks, upload each as a SEPARATE
            # asset, do NOT merge (the merge step would re-fetch the
            # full payload via getInfo() and hit the same 10 MB cap).
            try:
                chunks = _split_geojson_features(geojson_dict)
            except ValueError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"特征文件分块失败：{exc}") from exc

            print(
                f"[ingest] 分块上传 {asset_id}："
                f"{len(features)} features, "
                f"{payload_bytes / 1024 / 1024:.2f} MB → "
                f"{len(chunks)} chunks (each ≤ "
                f"{_MAX_GEE_UPLOAD_BYTES / 1024 / 1024:.0f} MB)"
            )

            chunk_asset_ids: List[str] = []
            for i, chunk in enumerate(chunks):
                chunk_asset_id = f"{asset_id}_chunk{i:03d}"
                try:
                    chunk_fc = ee.FeatureCollection(chunk)
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(
                        f"分块 {i + 1}/{len(chunks)} 构造 ee.FeatureCollection 失败：{exc}"
                    ) from exc
                task = ee.batch.Export.table.toAsset(
                    collection=chunk_fc,
                    description=f"{description}_chunk{i:03d}",
                    assetId=chunk_asset_id,
                )
                task.start()
                chunk_asset_ids.append(chunk_asset_id)
                print(
                    f"[ingest]   chunk {i + 1}/{len(chunks)} submitted: "
                    f"{chunk_asset_id}"
                )

            # Chunks must be queryable before the public caller polls
            # ``_wait_for_asset`` on each one. Raises TimeoutError on
            # failure; the algorithm-level error label still reads
            # "上传到 GEE 失败：<reason>".
            for ca in chunk_asset_ids:
                _wait_for_asset(ca, timeout=600.0)
            print(f"[ingest] 分块上传全部完成：{len(chunk_asset_ids)} chunks")
            return chunk_asset_ids
    else:
        # SHP path — fc already built above.
        if fc is None or not isinstance(fc, ee.FeatureCollection):
            raise RuntimeError(
                "geemap 未返回 ee.FeatureCollection，请检查输入文件格式；"
                " 必要时直接在 GEE Code Editor 上传后填 Asset 路径。"
            )
        task = ee.batch.Export.table.toAsset(
            collection=fc,
            description=description,
            assetId=asset_id,
        )
        task.start()
        return [asset_id]


def _wait_for_asset(asset_id: str, *, timeout: float = 240.0) -> None:
    """Block until the ingested asset is queryable.

    We poll by trying `ee.data.getAsset(asset_id)` — the asset only becomes
    visible there after ingestion completes. Poll interval kept short to
    fail fast on access-denied (the user-facing message in the calling
    algorithm then points at the auth mismatch path).
    """
    import ee

    _POLL_INTERVAL = 2.0
    deadline = time.monotonic() + timeout
    last_err: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            ee.data.getAsset(asset_id)
            return  # success
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(_POLL_INTERVAL)
    raise TimeoutError(
        f"GEE 资产上传超时（>{int(timeout)}s）：{asset_id}; last={last_err}"
    )


# ---- Public API -----------------------------------------------------------


def ensure_asset_for_local(
    *,
    local_path: str,
    slot: str,
    algorithm: str,
    project: Optional[str],
    kind_hint: Optional[str] = None,
) -> List[str]:
    """Return GEE asset ids for `local_path`, ingesting it if necessary.

    The return type is always a list:

    - Single-element ``[asset_id]`` for small files / SHP that fit
      inside GEE's 10 MB ``Export.table.toAsset`` cap.
    - Multi-element list of ``<asset_id>_chunkNNN`` ids for CSV / GeoJSON
      that had to be chunked. **The caller must flatten them server-side
      at use time**:

          ee.FeatureCollection(asset_ids).flatten()

      GEE's ``Export.table.toAsset`` request body has a hard 10 MB cap
      and we cannot merge them back into a single asset client-side (that
      would just re-upload the same data). See the module docstring.

    See the module docstring for the cache layout and kind inference.

    Parameters
    ----------
    local_path : str
        Absolute path to a .geojson / .json / .csv / .shp / .zip file
        (vector) or .tif / .tiff (raster). For .csv the file must have
        `longitude` and `latitude` columns in the header. For .shp the
        sidecar files (.dbf/.shx/.prj) should sit in the same directory;
        if you upload them via webkit-directory multi-select, the directory
        layout will be preserved by ``app.api.uploads``. Raster files
        (.tif/.tiff) are not supported in-process and will raise a clear
        error pointing the user to the GEE Code Editor.
    slot : str
        Parameter slot name (e.g. "roi"). Used for cache scoping.
    algorithm : str
        Algorithm id (e.g. "crop_threshold"). Used for cache scoping and
        asset naming.
    project : str | None
        GEE project id. If None/empty, ingestion will fail because we
        can't build a unique asset name.
    kind_hint : str | None
        Force the asset type. None = auto-detect by extension.

    Returns
    -------
    list[str]
        One or more full GEE asset ids
        (``projects/<project>/assets/<algo>_<slot>_<8hex>`` and chunk
        variants for chunked uploads).
    """
    if not project:
        raise ValueError("缺少 GEE 项目 ID（gee_project），无法注册本地文件。")

    src = Path(local_path)
    if not src.exists():
        raise FileNotFoundError(f"本地研究区文件不存在：{src}")

    file_id = _derive_file_id(src, algorithm, slot)
    cache = _load_cache(algorithm, slot)
    cached = cache.get(file_id)
    # `_load_cache` already drops non-dict values, but assert it again here
    # so a future refactor can't reintroduce the "str object has no
    # attribute 'items'" failure mode.
    if isinstance(cached, dict) and cached.get("project") == project:
        # New cache layout: ``asset_ids`` is the canonical field.
        cached_ids = cached.get("asset_ids")
        if cached_ids:
            print(
                f"[ingest] 复用已有 GEE 资产 {list(cached_ids)} "
                f"(file_id={file_id})"
            )
            return list(cached_ids)
        # Backward-compat: legacy entries from before the chunked-upload
        # refactor stored a single ``asset_id`` string. Return it as a
        # one-element list so callers don't have to special-case the
        # cache-load path.
        legacy_id = cached.get("asset_id")
        if legacy_id:
            print(
                f"[ingest] 复用旧缓存 GEE 资产 {legacy_id} "
                f"(file_id={file_id}, legacy single-id format)"
            )
            return [legacy_id]

    kind = _infer_kind(src, kind_hint)
    asset_id = _make_asset_name(algorithm, slot, project)

    print(f"[ingest] 注册本地研究区 → base={asset_id} (kind={kind})")
    asset_ids = _ingest_vector_via_geemap(asset_id=asset_id, local_path=src, kind=kind)
    # Wait for all uploaded chunks (single upload → [asset_id], chunked →
    # multiple). If any times out the existing TimeoutError surfaces as
    # "上传到 GEE 失败" in the algorithm-level error label.
    for aid in asset_ids:
        _wait_for_asset(aid, timeout=600.0)

    cache[file_id] = {
        "asset_ids": list(asset_ids),
        # Backward-compat field — older callers / tooling may still read
        # this for display.
        "asset_id": asset_ids[0] if asset_ids else None,
        "project": project,
        "kind": kind,
        "local_path": str(src),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _save_cache(algorithm, slot, cache)
    print(f"[ingest] 上传完成 → {asset_ids}")
    return list(asset_ids)


__all__ = ["ensure_asset_for_local"]