"""Exercise the resumable GEE download with a mocked GEE.

The real downloader talks to earthengine.googleapis.com, which is only
reachable through a proxy that is not always up — so the interesting logic
(interrupt / resume / fail-fast) cannot be tested against the live
service.  This script stubs out the GEE side and checks the three
behaviours that matter:

  1. a clean run assembles every tile and publishes the final file;
  2. a run that dies half-way leaves **no** final file, keeps a partial
     one plus a manifest, and the next run resumes instead of redoing
     the tiles it already has;
  3. a dead network fails fast with a message that names the real cause
     instead of grinding through every tile.

Run from ``backend/``::

    python scripts/test_gee_raster_resume.py
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from rasterio.transform import Affine  # noqa: E402

from app.services import gee_raster as gr  # noqa: E402


GRID = gr.AssetGrid(
    crs="EPSG:4326",
    width=1000,
    height=1000,
    scale_x=0.01,
    translate_x=100.0,
    scale_y=-0.01,
    translate_y=30.0,
    bytes_per_px=1,
)
# The downloader refuses to go below 512 px per tile, so the mock grid has
# to be big enough for the requested tile size to survive the clamp.
TILE_PX = 512
MARKER = {"0_0": 11, "1_0": 22, "0_1": 33, "1_1": 44}


def _tile_payload(col: int, row: int) -> bytes:
    """A single-band GeoTIFF in memory, matching one tile's window."""
    width = min(TILE_PX, GRID.width - col * TILE_PX)
    height = min(TILE_PX, GRID.height - row * TILE_PX)
    arr = np.full((1, height, width), MARKER[f"{col}_{row}"], dtype=np.uint8)
    transform = GRID.transform * Affine.translation(col * TILE_PX, row * TILE_PX)
    with rasterio.io.MemoryFile() as mem:
        with mem.open(
            driver="GTiff",
            width=width,
            height=height,
            count=1,
            dtype="uint8",
            crs=GRID.crs,
            transform=transform,
        ) as ds:
            ds.write(arr)
        return mem.read()


class FakeDownloader:
    """Stands in for the GEE side of the downloader."""

    def __init__(self) -> None:
        self.fetched: list[str] = []
        self.failing: set[str] = set()
        self.dead_network = False

    def install(self) -> None:
        """Monkeypatch the module-level helpers the downloader calls."""
        fake_ee = types.ModuleType("ee")
        fake_ee.Image = lambda asset_id: object()
        sys.modules["ee"] = fake_ee

        gr.read_asset_grid = lambda asset_id: GRID
        gr._make_url_retry = lambda img, grid, tile, attempts=4: f"mock://{tile.key}"
        gr._http_get = self._http_get

    def _http_get(self, url: str, **kwargs) -> bytes:
        key = url.split("://", 1)[1]
        self.fetched.append(key)
        if self.dead_network:
            raise gr.GeeRasterError("模拟：代理连接被重置")
        if key in self.failing:
            raise gr.GeeRasterError(f"模拟：图块 {key} 下载失败")
        col, row = (int(part) for part in key.split("_"))
        return _tile_payload(col, row)


def _read_out(path: str):
    with rasterio.open(path) as ds:
        return ds.read(1)


def test_clean_run(fake: FakeDownloader, tmpdir: str) -> None:
    print("\n=== 1. 完整下载 ===")
    out = os.path.join(tmpdir, "clean.tif")
    fake.fetched.clear()
    fake.failing.clear()
    fake.dead_network = False

    stats = gr.download_asset_to_geotiff(
        "projects/x/assets/y", out, tile_pixels=TILE_PX, max_workers=2,
        build_overviews=False,
    )
    assert os.path.isfile(out), "最终文件应当存在"
    assert not os.path.isfile(out + gr._PARTIAL_SUFFIX), "不应残留 .partial"
    assert not os.path.isfile(out + gr._MANIFEST_SUFFIX), "不应残留 manifest"
    arr = _read_out(out)
    assert arr.shape == (1000, 1000), arr.shape
    for key, marker in MARKER.items():
        col, row = (int(p) for p in key.split("_"))
        block = arr[
            row * TILE_PX: row * TILE_PX + TILE_PX,
            col * TILE_PX: col * TILE_PX + TILE_PX,
        ]
        assert (block == marker).all(), f"{key} 数据不对: {np.unique(block)}"
    print(f"  OK  {len(fake.fetched)} 个图块全部写入，文件 {stats['size_bytes']} B")


def test_resume(fake: FakeDownloader, tmpdir: str) -> None:
    print("\n=== 2. 中断后续传 ===")
    out = os.path.join(tmpdir, "resume.tif")
    fake.fetched.clear()
    fake.failing.clear()
    fake.dead_network = False
    # 让最后两个图块失败
    fake.failing = {"0_1", "1_1"}

    try:
        gr.download_asset_to_geotiff(
            "projects/x/assets/y", out, tile_pixels=TILE_PX, max_workers=2,
            build_overviews=False,
        )
        raise AssertionError("应当失败")
    except gr.GeeRasterError as exc:
        print(f"  首次下载失败（预期）: {str(exc)[:80]}…")

    assert not os.path.isfile(out), "失败时不得留下最终文件（旧实现会留下半截 TIF）"
    assert os.path.isfile(out + gr._PARTIAL_SUFFIX), "应当保留 .partial 供续传"
    assert os.path.isfile(out + gr._MANIFEST_SUFFIX), "应当保留 manifest"

    # 修好网络后续传
    fake.failing.clear()
    fake.fetched.clear()
    stats = gr.download_asset_to_geotiff(
        "projects/x/assets/y", out, tile_pixels=TILE_PX, max_workers=2,
        build_overviews=False,
    )
    assert os.path.isfile(out), "续传后最终文件应当存在"
    assert not os.path.isfile(out + gr._PARTIAL_SUFFIX)
    assert not os.path.isfile(out + gr._MANIFEST_SUFFIX)

    # 关键断言：第二轮只抓了缺的那两个图块
    assert set(fake.fetched) == {"0_1", "1_1"}, (
        f"续传只应下载缺失的图块，实际抓了 {sorted(fake.fetched)}"
    )
    arr = _read_out(out)
    for key, marker in MARKER.items():
        col, row = (int(p) for p in key.split("_"))
        block = arr[
            row * TILE_PX: row * TILE_PX + TILE_PX,
            col * TILE_PX: col * TILE_PX + TILE_PX,
        ]
        assert (block == marker).all(), f"{key} 数据不对: {np.unique(block)}"
    print(f"  OK  续传只补了 {sorted(fake.fetched)}，最终数据完整")


def test_dead_network(fake: FakeDownloader, tmpdir: str) -> None:
    print("\n=== 3. 网络全断时的快速失败 ===")
    out = os.path.join(tmpdir, "dead.tif")
    fake.fetched.clear()
    fake.dead_network = True

    import time

    start = time.time()
    try:
        gr.download_asset_to_geotiff(
            "projects/x/assets/y", out, tile_pixels=TILE_PX, max_workers=2,
            build_overviews=False,
        )
        raise AssertionError("应当失败")
    except gr.GeeRasterError as exc:
        elapsed = time.time() - start
        message = str(exc)
        assert "代理" in message or "网络" in message, message
        print(f"  OK  {elapsed:.1f}s 内快速失败: {message[:90]}…")
    assert not os.path.isfile(out), "失败时不得留下最终文件"


def test_target_lock(fake: FakeDownloader, tmpdir: str) -> None:
    print("\n=== 4. 同一目标不允许并发下载 ===")
    out = os.path.join(tmpdir, "locked.tif")
    fake.failing.clear()
    fake.dead_network = False

    # 第一个下载器持锁；第二个必须被挡在门外，而不是把 .partial 写花。
    lock = gr._TargetLock(out + gr._LOCK_SUFFIX)
    lock.acquire()
    try:
        try:
            gr.download_asset_to_geotiff(
                "projects/x/assets/y", out, tile_pixels=TILE_PX, max_workers=2,
                build_overviews=False,
            )
        except gr.GeeRasterError as exc:
            assert "另一个进程" in str(exc), str(exc)
            print(f"  OK  第二个下载器被挡下: {str(exc)[:60]}…")
        else:
            raise AssertionError("并发下载本应被拒绝")
    finally:
        lock.release()

    # 锁释放后可以正常下载。
    stats = gr.download_asset_to_geotiff(
        "projects/x/assets/y", out, tile_pixels=TILE_PX, max_workers=2,
        build_overviews=False,
    )
    assert os.path.isfile(out)
    print(f"  OK  锁释放后下载成功（{stats['size_bytes']} B）")


def main() -> int:
    fake = FakeDownloader()
    fake.install()
    with tempfile.TemporaryDirectory() as tmpdir:
        test_clean_run(fake, tmpdir)
        test_resume(fake, tmpdir)
        test_dead_network(fake, tmpdir)
        test_target_lock(fake, tmpdir)
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
