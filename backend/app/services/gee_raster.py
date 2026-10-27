"""Tiled download of a GEE Image Asset into a local GeoTIFF.

Why this module exists
----------------------
GEE's ``getDownloadURL`` serves a single request, and the service enforces
two hard limits on it:

  * the pixel grid may not exceed **32768 px per side**;
  * the request payload may not exceed **48 MiB** (50 331 648 bytes).

Large exports therefore cannot be fetched with one call.  The Guangxi
sugarcane harvest mask is the concrete example: its asset grid is
**84 720 x 59 251 = 5.02 G pixels**, so a single-shot download dies with

    Pixel grid dimensions (84722x59287) must be less than or equal to 32768.

``geemap.ee_export_image`` — the helper the algorithms used to call — issues
exactly one such request, so it cannot rescue these assets either.

How this module works
---------------------
1. The asset's *own* pixel grid is read from ``ee.data.getAsset``.  That
   grid is the ground truth: it is the exact affine the export was written
   with.
2. The grid is cut into tiles.  Each tile is requested with
   ``crs_transform`` + ``dimensions``, which pins the server-side rendering
   to one exact affine window.  The returned tiles therefore land *exactly*
   on the asset's pixel grid — no resampling, no drift between neighbours.
3. Every tile is pasted into one output GeoTIFF at its window position.

The GEE lock
------------
``getDownloadURL`` has to run against the job's own project binding, and
the binding is process-global.  The caller is expected to hold a
``gee_session(project)`` for the duration (the job service does this around
every ``finalize`` call).  The slow part of the work — the HTTP transfer
of the tile payloads — needs no EE binding at all, so it runs inside
``gee_idle()``, which hands the process-wide lock back to other jobs while
the bytes are in flight.  That keeps a multi-minute download from blocking
every other GEE job in the process.

Memory
------
Tiles are fetched and written in bounded batches, so peak memory stays at
``max_workers * tile_bytes`` regardless of how large the asset is.
"""
from __future__ import annotations

import io
import json
import os
import random
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from app.services.gee_auth import gee_idle


# --- GEE service limits (see module docstring) ----------------------------
_MAX_GRID_PX = 32768
_MAX_REQUEST_BYTES = 50_331_648  # 48 MiB

# Default tile edge in pixels.  4096 x 4096 of an INT band is ~33 MB,
# comfortably under the 48 MiB request cap, and 4096 <= 32768.
_DEFAULT_TILE_PX = 4096

# Bytes per pixel assumed per band precision, used to shrink the tile size
# for wide dtypes (DOUBLE would blow the 48 MiB cap at 4096).
_BYTES_PER_PX = {"INT": 2, "FLOAT": 4, "DOUBLE": 8}

_MIN_TILE_PX = 512
_DEFAULT_WORKERS = 4
# Per-request timeout for one tile transfer.  A tile is at most 48 MiB, so
# 5 minutes is already very generous — the point of the cap is that a
# *hung* proxy connection must not park a worker thread for 15 minutes.
_DEFAULT_TIMEOUT = 300

# --- resilience knobs -------------------------------------------------------
# A tile transfer is retried this many times before the tile is parked for
# the next round.  8 attempts with exponential backoff spans ~4 minutes,
# which is what it takes for a flaky local proxy (Clash & co.) to come back.
_HTTP_ATTEMPTS = 8

# How many times the whole batch may be replayed before giving up.  Each
# round re-requests only the tiles that failed, so a proxy outage costs a
# pause, not the whole download.
_MAX_ROUNDS = 6

# Backoff between rounds (seconds).  The proxy needs a moment to recover.
_ROUND_BACKOFF = 15.0

# Suffixes for the resumable download.  The raster is assembled in
# ``<target>.partial`` and only renamed onto ``<target>`` once every tile
# has arrived, so a half-finished download can never masquerade as a
# finished result.
_PARTIAL_SUFFIX = ".partial"
_MANIFEST_SUFFIX = ".tiles.json"
# Exclusive lock file guarding one target.  OS-released, so a crashed
# download never leaves a stale lock behind.
_LOCK_SUFFIX = ".partial.lock"

# HTTP statuses worth retrying.  Everything else in the 4xx range is a
# request problem that will not fix itself.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class GeeRasterError(RuntimeError):
    """Raised when an asset cannot be turned into a local GeoTIFF."""


# ---------------------------------------------------------------------------
# Asset grid
# ---------------------------------------------------------------------------
class AssetGrid:
    """The pixel grid an asset was exported on."""

    def __init__(
        self,
        *,
        crs: str,
        width: int,
        height: int,
        scale_x: float,
        translate_x: float,
        scale_y: float,
        translate_y: float,
        bytes_per_px: int = 2,
    ) -> None:
        self.crs = crs
        self.width = width
        self.height = height
        self.scale_x = scale_x
        self.translate_x = translate_x
        self.scale_y = scale_y
        self.translate_y = translate_y
        self.bytes_per_px = bytes_per_px

    @property
    def transform(self) -> Any:
        """rasterio Affine of the asset grid (north-up for EPSG:4326)."""
        from rasterio.transform import Affine

        return Affine(
            self.scale_x, 0.0, self.translate_x,
            0.0, self.scale_y, self.translate_y,
        )

    def max_tile_px(self, requested: int) -> int:
        """Largest tile edge that respects both GEE request limits."""
        cap_by_bytes = int((_MAX_REQUEST_BYTES / max(1, self.bytes_per_px)) ** 0.5)
        tile = min(int(requested), _MAX_GRID_PX, cap_by_bytes)
        return max(_MIN_TILE_PX, tile)

    def pixel_bounds(self, col: int, row: int) -> Tuple[float, float, float, float]:
        """(left, bottom, right, top) of a pixel in CRS units."""
        left = self.translate_x + col * self.scale_x
        top = self.translate_y + row * self.scale_y
        right = left + self.scale_x
        bottom = top + self.scale_y
        return (min(left, right), min(bottom, top),
                max(left, right), max(bottom, top))


def read_asset_grid(asset_id: str) -> AssetGrid:
    """Read the exported pixel grid of a GEE Image Asset.

    Raises:
        GeeRasterError: when the asset has no band / no grid information.
    """
    import ee

    try:
        info = ee.data.getAsset(asset_id)
    except Exception as exc:  # noqa: BLE001
        raise GeeRasterError(f"读取 GEE 资产失败（{asset_id}）：{exc}") from exc

    bands = (info or {}).get("bands") or []
    if not bands:
        raise GeeRasterError(f"GEE 资产没有波段信息：{asset_id}")

    band = bands[0]
    grid = band.get("grid") or {}
    dims = grid.get("dimensions") or {}
    aff = grid.get("affineTransform") or {}

    width = int(dims.get("width") or 0)
    height = int(dims.get("height") or 0)
    scale_x = float(aff.get("scaleX") or 0.0)
    scale_y = float(aff.get("scaleY") or 0.0)
    translate_x = float(aff.get("translateX") or 0.0)
    translate_y = float(aff.get("translateY") or 0.0)
    crs = grid.get("crsCode") or "EPSG:4326"

    if width <= 0 or height <= 0 or scale_x == 0.0 or scale_y == 0.0:
        raise GeeRasterError(
            f"GEE 资产的网格信息不完整：{asset_id}（{width}x{height}, "
            f"scale={scale_x}/{scale_y}）"
        )

    precision = ((band.get("dataType") or {}).get("precision") or "INT").upper()
    bytes_per_px = _BYTES_PER_PX.get(precision, 2)

    return AssetGrid(
        crs=crs,
        width=width,
        height=height,
        scale_x=scale_x,
        translate_x=translate_x,
        scale_y=scale_y,
        translate_y=translate_y,
        bytes_per_px=bytes_per_px,
    )


# ---------------------------------------------------------------------------
# Tile plan
# ---------------------------------------------------------------------------
class Tile:
    """One rectangular slice of the asset grid."""

    __slots__ = ("col", "row", "col_off", "row_off", "width", "height")

    def __init__(
        self, col: int, row: int, col_off: int, row_off: int, width: int, height: int
    ) -> None:
        self.col = col
        self.row = row
        self.col_off = col_off
        self.row_off = row_off
        self.width = width
        self.height = height

    @property
    def key(self) -> str:
        return f"{self.col}_{self.row}"


def plan_tiles(grid: AssetGrid, tile_px: int) -> List[Tile]:
    """Cut the asset grid into ``tile_px``-sided tiles (edge tiles smaller)."""
    tiles: List[Tile] = []
    n_cols = (grid.width + tile_px - 1) // tile_px
    n_rows = (grid.height + tile_px - 1) // tile_px
    for row in range(n_rows):
        row_off = row * tile_px
        th = min(tile_px, grid.height - row_off)
        for col in range(n_cols):
            col_off = col * tile_px
            tw = min(tile_px, grid.width - col_off)
            tiles.append(Tile(col, row, col_off, row_off, tw, th))
    return tiles


def tile_crs_transform(grid: AssetGrid, tile: Tile) -> List[float]:
    """Affine of one tile, in GEE's ``crs_transform`` order.

    GEE wants ``[xScale, xShearing, xTranslation, yShearing, yScale,
    yTranslation]`` — the same layout as a rasterio Affine.
    """
    left, _bottom, _right, top = grid.pixel_bounds(tile.col_off, tile.row_off)
    return [
        grid.scale_x, 0.0, left,
        0.0, grid.scale_y, top,
    ]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
_SESSION_LOCAL = threading.local()


# ---------------------------------------------------------------------------
# Target lock — one downloader per target file
# ---------------------------------------------------------------------------


class _TargetLock:
    """An exclusive, OS-released lock on one download target.

    Two processes downloading the same asset into the same ``.partial`` file
    corrupt it: both open the raster ``"r+"``, both write tiles, and each
    rewrites the TIFF directory on close.  The symptom is a file that looks
    complete but has a mangled header — and a publish step that fails with
    a confusing "file is in use" error.

    The lock is an OS file lock, not a flag file: the kernel releases it when
    the process dies, so a crashed download never leaves a stale lock
    behind.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> None:
        self._fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fh.close()
            self._fh = None
            raise GeeRasterError(
                f"另一个进程正在下载同一个文件：{self.path}。"
                "请等它跑完，或停掉那个进程后再试。"
            ) from exc

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def __enter__(self) -> "_TargetLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


def _http_session() -> requests.Session:
    """A per-thread ``requests.Session`` (connection reuse + proxy aware).

    One session per thread keeps the TCP connection to the local proxy
    alive between tiles.  That matters when the proxy is the flaky part:
    every fresh TLS handshake is another chance for it to reset the
    connection, and GEE's thumbnail endpoint is reached through it.
    """
    session = getattr(_SESSION_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=8, pool_maxsize=8, max_retries=0
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _SESSION_LOCAL.session = session
    return session


def _is_transient_error(exc: BaseException) -> bool:
    """Is this failure worth retrying?

    Connection resets, timeouts and proxy errors are the signature of a
    flaky local proxy — they clear up on their own, so retrying is the
    right move.  A 404 or a malformed request will fail identically no
    matter how often it is repeated, so it must not burn the retry budget.
    """
    if isinstance(
        exc,
        (
            requests.exceptions.ConnectionError,   # includes ProxyError
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ContentDecodingError,
            requests.exceptions.SSLError,
        ),
    ):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        return status in _RETRYABLE_STATUS
    return False


def _http_get(
    url: str, *, timeout: int = _DEFAULT_TIMEOUT, max_attempts: int = _HTTP_ATTEMPTS
) -> bytes:
    """GET a GEE download URL, retrying transient errors with backoff.

    Two failure modes have to be survived here:

      * **HTTP 429** — GEE throttles ``getPixels`` bursts.  Backing off and
        retrying is the only way through.
      * **Connection reset / proxy hiccup** — the tile URL is served
        through the machine's system proxy, and that proxy occasionally
        drops long-lived connections mid-transfer.  The download used to
        give up after 6 quick attempts, which turned a 30-second proxy
        blip into a failed job and a discarded 5 GB download.
    """
    delay = 2.0
    last_error: Optional[BaseException] = None
    attempts_made = 0

    for attempt in range(1, max_attempts + 1):
        attempts_made = attempt
        try:
            resp = _http_session().get(url, timeout=timeout)
            status = resp.status_code
            if status in _RETRYABLE_STATUS:
                last_error = RuntimeError(f"GEE 返回 HTTP {status}")
            elif status >= 400:
                # A permanent error (404, 403, 400) — fail fast, retrying
                # cannot help.
                resp.raise_for_status()
                return resp.content
            else:
                return resp.content
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if not _is_transient_error(exc):
                break
        if attempt < max_attempts:
            # Exponential backoff with jitter so parallel workers don't
            # retry in lockstep.
            time.sleep(delay + random.uniform(0.0, 1.5))
            delay = min(delay * 2.0, 60.0)

    raise GeeRasterError(
        f"下载 GEE 图块失败（尝试 {attempts_made} 次）：{last_error}"
    )


def _unwrap_tile(payload: bytes) -> bytes:
    """Return the GeoTIFF bytes out of a GEE download response.

    ``filePerBand=False`` returns a ZIP holding one GeoTIFF; some responses
    are the GeoTIFF itself.  Handle both.
    """
    if payload[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = [
                n for n in zf.namelist()
                if n.lower().endswith((".tif", ".tiff"))
            ]
            if not names:
                raise GeeRasterError("GEE 下载结果里没有 GeoTIFF")
            # Prefer the first band file; a single-band asset has exactly one.
            return zf.read(names[0])
    return payload


# ---------------------------------------------------------------------------
# Tile IO
# ---------------------------------------------------------------------------
def _read_tile(payload: bytes) -> Tuple[Any, Any, Any]:
    """Decode a tile payload into ``(array, transform, nodata)``.

    The array keeps every band: shape ``(bands, height, width)``.  The
    dtype is whatever GEE produced (uint8 for masks, int16 for class
    codes, float32 for continuous rasters) and is reused for the output
    raster, so nothing is silently truncated.
    """
    import rasterio
    from rasterio.io import MemoryFile

    raw = _unwrap_tile(payload)
    with MemoryFile(raw) as mem:
        with mem.open() as src:
            arr = src.read()
            transform = src.transform
            nodata = src.nodata
    return arr, transform, nodata


def _window_for_tile(dst_transform: Any, tile_transform: Any) -> Tuple[int, int]:
    """Locate a tile inside the output grid, from the tile's own transform.

    Deriving the window from the tile's transform (rather than trusting
    the requested offsets) means a server-side adjustment shows up as an
    error instead of silently mis-placed pixels.
    """
    col_off = round((tile_transform.c - dst_transform.c) / dst_transform.a)
    row_off = round((tile_transform.f - dst_transform.f) / dst_transform.e)
    return int(col_off), int(row_off)


def _check_scales_match(dst_transform: Any, tile_transform: Any, key: str) -> None:
    for attr, dst_a, tile_a in (
        ("a", dst_transform.a, tile_transform.a),
        ("e", dst_transform.e, tile_transform.e),
    ):
        if abs(dst_a - tile_a) > max(abs(dst_a), 1e-12) * 1e-6:
            raise GeeRasterError(
                f"图块 {key} 的仿射与资产网格不一致（{attr}: {tile_a} vs {dst_a}），"
                "拒绝写入错位的数据"
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def _make_url(img: Any, grid: AssetGrid, tile: Tile) -> str:
    """Ask GEE for the download URL of one tile."""
    return img.getDownloadURL(
        {
            "name": f"tile_{tile.col}_{tile.row}",
            "filePerBand": False,
            "crs": grid.crs,
            "crs_transform": tile_crs_transform(grid, tile),
            "dimensions": [tile.width, tile.height],
        }
    )


def _make_url_retry(
    img: Any, grid: AssetGrid, tile: Tile, *, attempts: int = 4
) -> str:
    """``_make_url`` with retries — the URL call rides the same proxy.

    ``getDownloadURL`` is a GEE API call, so it fails the same way the
    tile transfer does when the proxy is having a bad minute.  A tile that
    cannot even get a URL is treated exactly like one whose bytes never
    arrived: parked for the next round.
    """
    delay = 2.0
    last_error: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return _make_url(img, grid, tile)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < attempts:
                time.sleep(delay + random.uniform(0.0, 1.0))
                delay = min(delay * 2.0, 30.0)
    raise GeeRasterError(f"获取 GEE 图块下载地址失败：{last_error}")


def _write_tile(
    dst: Any,
    out_transform: Any,
    tile: Tile,
    arr: Any,
    tile_transform: Any,
) -> None:
    """Paste one tile into the output raster at its window position."""
    from rasterio.windows import Window

    _check_scales_match(out_transform, tile_transform, tile.key)
    col_off, row_off = _window_for_tile(out_transform, tile_transform)
    height, width = arr.shape[-2], arr.shape[-1]
    window = Window(col_off, row_off, width, height)
    if arr.ndim == 2:
        dst.write(arr, 1, window=window)
        return
    for band in range(arr.shape[0]):
        dst.write(arr[band], band + 1, window=window)


# ---------------------------------------------------------------------------
# Resume manifest
# ---------------------------------------------------------------------------
def _grid_signature(grid: AssetGrid, tile_px: int) -> Dict[str, Any]:
    """Identity of a download: same asset grid + same tiling.

    Stored in the manifest so a resumed download can prove it is
    continuing the *same* raster.  A mismatch (asset re-exported at a
    different scale, different tile size) means the partial file cannot
    be reused and the download restarts from scratch.
    """
    return {
        "crs": grid.crs,
        "width": grid.width,
        "height": grid.height,
        "scale_x": grid.scale_x,
        "scale_y": grid.scale_y,
        "translate_x": grid.translate_x,
        "translate_y": grid.translate_y,
        "tile_px": tile_px,
    }


def _load_manifest(manifest_path: str, signature: Dict[str, Any]) -> Optional[set]:
    """Read the set of already-downloaded tile keys, or ``None`` if unusable."""
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("signature") != signature:
        return None
    completed = payload.get("completed")
    if not isinstance(completed, list):
        return None
    return {str(key) for key in completed}


def _save_manifest(
    manifest_path: str, signature: Dict[str, Any], completed: set
) -> None:
    """Write the manifest atomically (temp file + rename).

    A manifest truncated by a crash would be read back as "no tiles
    done", which would throw away a perfectly good partial download.
    """
    tmp_path = manifest_path + ".tmp"
    payload = {"signature": signature, "completed": sorted(completed)}
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, manifest_path)


def _batches(items: List[Tile], size: int) -> List[List[Tile]]:
    """Split ``items`` into consecutive batches of at most ``size``."""
    return [items[i:i + size] for i in range(0, len(items), size)]


def _publish(partial_path: str, final_path: str, attempts: int = 5) -> None:
    """Move the finished raster onto its final name, retrying Windows locks.

    ``os.replace`` raises ``PermissionError`` (WinError 32) on Windows when
    the *destination* is still open somewhere — the preview service's LRU
    cache, an Explorer preview pane, a virus scanner.  Those handles go
    away within seconds, so a short retry loop turns a lost download into a
    one-second delay.
    """
    delay = 1.0
    last_error: Optional[OSError] = None
    for attempt in range(1, attempts + 1):
        try:
            os.replace(partial_path, final_path)
            return
        except PermissionError as exc:  # WinError 32 / 5
            last_error = exc
            if attempt < attempts:
                time.sleep(delay)
                delay = min(delay * 2.0, 8.0)
    raise GeeRasterError(
        f"下载完成但无法把结果文件改名到位：{final_path}"
        f"（{last_error}）。请关闭正在打开该文件的程序后重跑 —— "
        f"已下载的图块会保留在 {partial_path}。"
    ) from last_error


def _profile_for(grid: AssetGrid, arr: Any, nodata: Optional[float]) -> Dict[str, Any]:
    """Build the output GeoTIFF profile from the first tile that arrives.

    The dtype and band count come from the data itself — GEE hands back
    uint8 for a byte asset, int16 for class codes, float32 for continuous
    rasters — so the output keeps the source precision instead of
    silently truncating it.
    """
    return {
        "driver": "GTiff",
        "width": grid.width,
        "height": grid.height,
        "count": 1 if arr.ndim == 2 else int(arr.shape[0]),
        "dtype": arr.dtype.name,
        "crs": grid.crs,
        "transform": grid.transform,
        "nodata": 0 if nodata is None else nodata,
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
        "compress": "deflate",
        "BIGTIFF": "IF_SAFER",
    }


def _last_error_text(errors: Dict[str, str]) -> str:
    """The most recent error text, trimmed for a user-facing message."""
    if not errors:
        return "（无详细错误）"
    return list(errors.values())[-1][:300]


def download_asset_to_geotiff(
    asset_id: str,
    local_path: str,
    *,
    tile_pixels: int = _DEFAULT_TILE_PX,
    max_workers: int = _DEFAULT_WORKERS,
    progress_callback: Optional[Callable[[int, str], None]] = None,
    build_overviews: bool = True,
) -> Dict[str, Any]:
    """Download a GEE Image Asset into a single GeoTIFF at ``local_path``.

    The download is tiled and aligned to the asset's own pixel grid, so it
    works for assets far larger than GEE's 32768 px / 48 MiB single-request
    limits.

    Resumability
    ------------
    The raster is assembled in ``<local_path>.partial`` and the set of
    tiles already written is tracked in ``<local_path>.tiles.json``.  A
    re-run picks up where the previous one stopped instead of starting
    over — which is the difference between a 5-minute and a 3-hour
    download when the machine's proxy drops every third connection.

    The finished raster is only moved onto ``local_path`` once **every**
    tile has arrived.  Before this existed, a download that died at tile
    200 of 315 left a GeoTIFF with the right geometry and 5 % of the data
    on disk, which the preview service happily rendered as an almost
    empty map.

    Args:
        asset_id: e.g. ``projects/<project>/assets/<name>``.
        local_path: destination ``.tif`` path (parent dirs are created).
        tile_pixels: requested tile edge; shrunk automatically when the
            band's dtype would exceed the request-size cap.
        max_workers: parallel tile transfers.
        progress_callback: ``(percent, message)`` progress reporter.
        build_overviews: build internal overviews so the preview service can
            read a downsampled view without decoding the whole raster.

    Returns:
        dict with the output raster's geometry and transfer statistics.

    Raises:
        GeeRasterError: on any unrecoverable GEE / IO failure.
    """
    import ee
    import rasterio

    final_path = os.path.abspath(local_path)
    os.makedirs(os.path.dirname(final_path), exist_ok=True)

    # One downloader per target file.  Two processes writing the same
    # ``.partial`` raster corrupt it (and fight over the final rename), so
    # the second one is turned away instead.
    lock = _TargetLock(final_path + _LOCK_SUFFIX)
    lock.acquire()
    try:
        return _download_locked(
            asset_id,
            final_path,
            tile_pixels=tile_pixels,
            max_workers=max_workers,
            progress_callback=progress_callback,
            build_overviews=build_overviews,
        )
    finally:
        lock.release()


def _download_locked(
    asset_id: str,
    final_path: str,
    *,
    tile_pixels: int,
    max_workers: int,
    progress_callback: Optional[Callable[[int, str], None]],
    build_overviews: bool,
) -> Dict[str, Any]:
    """The body of :func:`download_asset_to_geotiff` (target already locked)."""
    import ee
    import rasterio

    grid = read_asset_grid(asset_id)
    tile_px = grid.max_tile_px(tile_pixels)
    tiles = plan_tiles(grid, tile_px)
    total = len(tiles)

    partial_path = final_path + _PARTIAL_SUFFIX
    manifest_path = final_path + _MANIFEST_SUFFIX

    signature = _grid_signature(grid, tile_px)
    completed: set = set()

    # --- resume? ---------------------------------------------------------
    if os.path.isfile(partial_path) and os.path.isfile(manifest_path):
        resumed = _load_manifest(manifest_path, signature)
        if resumed:
            try:
                with rasterio.open(partial_path) as probe:
                    if (
                        probe.width != grid.width
                        or probe.height != grid.height
                    ):
                        resumed = None
            except Exception:  # noqa: BLE001
                resumed = None
        if resumed is not None:
            completed = resumed
        else:
            # The partial file cannot be trusted (mismatched grid, missing
            # manifest, unreadable) — start over.
            for stale in (partial_path, manifest_path):
                if os.path.isfile(stale):
                    try:
                        os.remove(stale)
                    except OSError:
                        pass

    pending = [t for t in tiles if t.key not in completed]

    if progress_callback:
        if completed:
            progress_callback(
                5,
                f"继续下载 GEE 栅格：{grid.width}×{grid.height} 像素，"
                f"共 {total} 个图块，已完成 {len(completed)} 个，"
                f"剩余 {len(pending)} 个",
            )
        else:
            progress_callback(
                5,
                f"准备下载 GEE 栅格：{grid.width}×{grid.height} 像素，"
                f"切分为 {total} 个图块（{tile_px}px/块）",
            )

    out_transform = grid.transform
    img = ee.Image(asset_id)

    bytes_in = 0
    dst = None
    profile: Optional[Dict[str, Any]] = None
    round_no = 0
    batch_size = max(1, max_workers * 2)

    # Resume case: reopen the partial raster **in place**.  Opening it with
    # "w" would truncate it and throw away every tile the previous run had
    # already paid for.
    if completed:
        dst = rasterio.open(partial_path, "r+")
        profile = {
            "count": int(dst.count),
            "dtype": dst.dtypes[0],
            "nodata": dst.nodata,
        }

    try:
        while pending:
            round_no += 1
            if round_no > _MAX_ROUNDS:
                raise GeeRasterError(
                    f"下载 GEE 栅格失败：仍有 {len(pending)} 个图块未取到"
                    f"（已重试 {_MAX_ROUNDS - 1} 轮）。"
                    "通常是本机代理 / 网络不稳定，请检查代理后重跑 —— "
                    "已下载的图块会被保留，下次从断点继续。"
                )

            failed: List[Tile] = []
            round_done = 0
            round_total = 0
            last_error_by_key: Dict[str, str] = {}

            for batch in _batches(pending, batch_size):
                round_total += len(batch)

                # Phase 1 — ask GEE for the download URLs.  This needs the
                # job's EE project binding, so it runs with the session
                # lock held (the caller owns it).
                url_by_key: Dict[str, Optional[str]] = {}
                with ThreadPoolExecutor(max_workers=min(max_workers, len(batch))) as ex:
                    futures = {
                        ex.submit(_make_url_retry, img, grid, tile): tile
                        for tile in batch
                    }
                    for fut in futures:
                        tile = futures[fut]
                        try:
                            url_by_key[tile.key] = fut.result()
                        except Exception:  # noqa: BLE001
                            url_by_key[tile.key] = None

                # Phase 2 — transfer the bytes.  Pure HTTP, no EE binding
                # needed, so hand the GEE lock back to other jobs meanwhile.
                with gee_idle():
                    with ThreadPoolExecutor(max_workers=max_workers) as ex:
                        futures2 = {
                            ex.submit(_http_get, url_by_key[tile.key]): tile
                            for tile in batch
                            if url_by_key.get(tile.key)
                        }
                        for fut in as_completed(futures2):
                            tile = futures2[fut]
                            try:
                                payload = fut.result()
                            except Exception as exc:  # noqa: BLE001
                                last_error_by_key[tile.key] = str(exc)
                                failed.append(tile)
                                continue
                            try:
                                arr, tile_transform, nodata = _read_tile(payload)
                            except Exception as exc:  # noqa: BLE001
                                last_error_by_key[tile.key] = str(exc)
                                failed.append(tile)
                                continue
                            if dst is None:
                                profile = _profile_for(grid, arr, nodata)
                                dst = rasterio.open(partial_path, "w", **profile)
                            _write_tile(dst, out_transform, tile, arr, tile_transform)
                            bytes_in += len(payload)
                            completed.add(tile.key)
                            round_done += 1

                # Tiles that never got a URL are simply missing from
                # futures2 — park them for the next round.
                for tile in batch:
                    if not url_by_key.get(tile.key) and tile.key not in completed:
                        last_error_by_key.setdefault(
                            tile.key, "未能获取图块下载地址（代理 / 网络错误）"
                        )
                        failed.append(tile)

                if progress_callback:
                    pct = 10 + int(70 * len(completed) / max(1, total))
                    progress_callback(
                        pct,
                        f"正在下载 GEE 栅格：{len(completed)}/{total} 个图块"
                        f"（{bytes_in / (1024 * 1024):.1f} MB）",
                    )

                # Persist after every batch: a crash (or a proxy outage that
                # kills the process) must not cost more than one batch.
                _save_manifest(manifest_path, signature, completed)

                # Fast-fail when the network is down: if *every* tile of
                # this batch failed, grinding through the remaining 300
                # tiles with the same broken proxy would waste hours.
                if round_total and round_done == 0 and len(failed) >= round_total:
                    raise GeeRasterError(
                        "下载 GEE 栅格失败：本机无法连接 GEE（代理 / 网络不通）。"
                        f"本轮 {round_total} 个图块全部失败，最后一个错误："
                        f"{_last_error_text(last_error_by_key)}。"
                        "请检查本机代理（如 Clash）是否正常工作后重跑 —— "
                        "已下载的图块会被保留，下次从断点继续。"
                    )

            pending = failed
            if pending:
                if progress_callback:
                    progress_callback(
                        min(89, 10 + int(70 * len(completed) / max(1, total))),
                        f"{len(pending)} 个图块本轮未取到，{_ROUND_BACKOFF:.0f}s 后重试"
                        f"（第 {round_no}/{_MAX_ROUNDS} 轮）",
                    )
                time.sleep(_ROUND_BACKOFF * round_no)
    finally:
        if dst is not None:
            dst.close()

    # --- overviews: make the preview service's downsampled read cheap ---
    if build_overviews:
        try:
            with rasterio.open(partial_path, "r+") as over_dst:
                factors = [f for f in (2, 4, 8, 16, 32) if grid.width // f >= 1]
                if factors:
                    from rasterio.enums import Resampling

                    over_dst.build_overviews(factors, Resampling.nearest)
        except Exception:  # noqa: BLE001
            # Overviews are an optimisation, not a correctness requirement —
            # GDAL falls back to reading the full raster.
            pass

    # The raster is complete — publish it under its real name.
    #
    # On Windows the rename fails with "file is in use" when anything still
    # holds the destination open (the preview service's LRU cache, a virus
    # scanner, an Explorer preview pane).  Those handles are released within
    # seconds, so retry briefly instead of losing the whole download.
    _publish(partial_path, final_path)
    try:
        os.remove(manifest_path)
    except OSError:
        pass

    size_bytes = os.path.getsize(final_path)
    if progress_callback:
        progress_callback(
            90,
            f"GEE 栅格下载完成：{grid.width}×{grid.height}，"
            f"{size_bytes / (1024 * 1024):.1f} MB",
        )

    return {
        "crs": grid.crs,
        "width": grid.width,
        "height": grid.height,
        "count": int((profile or {}).get("count", 1)),
        "dtype": (profile or {}).get("dtype", "uint8"),
        "nodata": (profile or {}).get("nodata", 0),
        "transform": list(out_transform)[:6],
        "tile_pixels": tile_px,
        "tile_count": total,
        "download_bytes": bytes_in,
        "size_bytes": size_bytes,
        "path": final_path,
    }


def wait_for_asset(
    asset_id: str,
    *,
    timeout: float = 1800.0,
    poll: float = 5.0,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> None:
    """Block until a GEE asset becomes queryable, or the timeout expires.

    A task can report COMPLETED a moment before the exported asset is
    readable, so every finalize hook waits for the asset rather than
    assuming it is there.

    Raises:
        TimeoutError: when the asset does not show up within ``timeout``.
    """
    import ee

    deadline = time.monotonic() + timeout
    start = time.monotonic()
    last_error: Optional[BaseException] = None

    while time.monotonic() < deadline:
        try:
            ee.data.getAsset(asset_id)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        if progress_callback:
            elapsed = int(time.monotonic() - start)
            progress_callback(50, f"等待 GEE 资产就绪：{asset_id}（已等待 {elapsed}s）")
        with gee_idle():
            time.sleep(poll)

    raise TimeoutError(
        f"等待 GEE 资产超时（>{int(timeout)}s）：{asset_id}；最后错误：{last_error}"
    )


__all__ = [
    "AssetGrid",
    "GeeRasterError",
    "Tile",
    "download_asset_to_geotiff",
    "plan_tiles",
    "read_asset_grid",
    "tile_crs_transform",
    "wait_for_asset",
]
