# -*- coding: utf-8 -*-
"""后端接口自测脚本（仅用标准库，不依赖 requests）。

用法（后端已启动时，在 backend 目录下）：
    venv/Scripts/python scripts/test_api.py

用例覆盖：健康检查、算法/数据列表、文件上传、真实估产算法完整运行、
结果下载、参数校验与路径穿越防护。
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://127.0.0.1:8000"
DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def call(method: str, path: str, body: dict | None = None, raw: bool = False):
    url = BASE + quote(path, safe="/")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            content = resp.read()
            return resp.status, content if raw else json.loads(content.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except Exception:
            detail = str(exc)
        return exc.code, detail


def upload_file(filename: str, content: bytes, folder: str = ""):
    """用标准库手工构造 multipart/form-data 上传请求。"""
    boundary = "----ClaudeTestBoundary42"
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"folder\"\r\n\r\n{folder}\r\n".encode("utf-8"),
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8") + content + b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    req = urllib.request.Request(BASE + "/api/upload", data=b"".join(parts), method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def check(name: str, ok: bool, extra: str = ""):
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name} {extra}")
    if not ok:
        sys.exit(1)


def main() -> None:
    # 1. 健康检查
    status, body = call("GET", "/health")
    check("health", status == 200 and body.get("status") == "ok")

    # 2. 算法与数据列表（三个真实算法：本地估产 + 两个 GEE 算法）
    status, algos = call("GET", "/api/algorithms")
    check("algorithms 列表", status == 200
          and {a["id"] for a in algos} == {"areaclassification", "future2", "predicted26"},
          f"({[a['id'] for a in algos]})")
    status, files = call("GET", "/api/data")
    check("data 列表", status == 200 and len(files) >= 11, f"({len(files)} 个)")
    names = [f["name"] for f in files]
    check("真实数据在列表中", "GX_County_Sugarcane_Monthly_2020_2025_with_Yield_Area.xlsx" in names
          and "月度影像_2026/2026_04_NDVI.tif" in names)

    # 3. 文件上传（上传后从服务器端清理，不污染数据目录）
    status, up = upload_file("上传测试.csv", "县名,单产\n测试县,5.2\n".encode("utf-8"), "")
    check("上传 csv", status == 200 and up["name"] == "上传测试.csv", str(up))
    if status == 200:
        os.remove(DATA_DIR / "上传测试.csv")
    status, up = upload_file("bad.exe", b"x")
    check("上传不支持的类型 -> 400", status == 400, str(up)[:80])
    status, up = upload_file("x.csv", b"x", folder="../evil")
    check("上传非法子目录 -> 400", status == 400, str(up)[:80])

    # 4. 真实估产算法完整运行（分块计算，约 1-5 分钟）
    status, r = call("POST", "/api/run", {
        "algorithm_id": "predicted26",
        "inputs": {
            "excel": "GX_County_Sugarcane_Monthly_2020_2025_with_Yield_Area.xlsx",
            "raster_sample": "月度影像_2026/2026_04_NDVI.tif",
            "cane_mask": "classification_April_1.tif",
            "target_month": "2026-06",
        },
    })
    check("predicted26 运行", status == 200 and r.get("metrics") and r.get("tables")
          and r.get("rasters") and r["rasters"][0]["png_url"], str(r.get("message", ""))[:60])
    run_id = r["run_id"]
    check("省份预测单产 3-8 t/亩",
          3 <= next(m["value"] for m in r["metrics"] if m["name"] == "全省平均预测单产") <= 8)

    # 5. 结果文件下载（png 预览 + tif + csv）
    png_url = r["rasters"][0]["png_url"]
    status, png_bytes = call("GET", png_url, raw=True)
    check("png 预览可访问", status == 200 and png_bytes[:4] == b"\x89PNG", png_url)
    status, _ = call("GET", r["rasters"][0]["tif_url"], raw=True)
    check("tif 可下载", status == 200)
    csv_url = next(t["csv_url"] for t in r["tables"] if t["csv_url"])
    status, csv_bytes = call("GET", csv_url, raw=True)
    check("csv 可下载", status == 200 and b"\xe5\x9c\xb0\xe5\x90\x8d" in csv_bytes[:200], csv_url)

    # 6. 错误用例
    status, err = call("POST", "/api/run", {"algorithm_id": "predicted26", "inputs": {}})
    check("缺必填参数 -> 400", status == 400, str(err)[:60])
    status, err = call("POST", "/api/run", {
        "algorithm_id": "predicted26",
        "inputs": {"excel": "GX_County_Sugarcane_Monthly_2020_2025_with_Yield_Area.xlsx",
                   "raster_sample": "GX_County_Sugarcane_Monthly_2020_2025_with_Yield_Area.xlsx",
                   "target_month": "2026-06"},
    })
    check("tif 参数传 xlsx -> 400", status == 400, str(err)[:60])
    status, err = call("POST", "/api/run", {
        "algorithm_id": "predicted26",
        "inputs": {"excel": "GX_County_Sugarcane_Monthly_2020_2025_with_Yield_Area.xlsx",
                   "raster_sample": "月度影像_2026/2026_04_NDVI.tif",
                   "target_month": "2026年6月"},
    })
    check("月份格式错误 -> 400", status == 400, str(err)[:60])
    status, err = call("POST", "/api/run", {"algorithm_id": "no_such_algo", "inputs": {}})
    check("不存在的算法 -> 404", status == 404)
    status, _ = call("GET", f"/api/outputs/{run_id}/../run.py")
    check("路径穿越 -> 404", status == 404)
    status, _ = call("GET", "/api/outputs/ffffffffffffffffffffffffffffffff/x.tif")
    check("不存在的文件 -> 404", status == 404)

    print("\n全部用例通过。")


if __name__ == "__main__":
    main()
