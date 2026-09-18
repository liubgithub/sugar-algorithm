# -*- coding: utf-8 -*-
"""算法注册表：扫描 algorithms/ 目录，按需加载算法模块。

算法规范：每个算法是 algorithms/ 下一个 .py 文件，暴露
    ALGO_META = {"id", "name", "description", "params": [{"name", "label", "type", "required"}]}
    def run(inputs: dict[str, Path], workdir: Path) -> dict

新增算法只需加一个文件，重启后端即生效，无需改动其他代码。
"""
import importlib.util
import sys
from pathlib import Path

from ..config import ALGORITHMS_DIR

# 模块命名空间与真实包 algorithms 隔离，避免与包 import 冲突
_MODULE_PREFIX = "algo_plugins."


def _module_name(algorithm_id: str) -> str:
    return _MODULE_PREFIX + algorithm_id


def _load_module(algorithm_id: str):
    """按文件名加载算法模块（不在注册表 import，避免污染全局）。

    模块会缓存在 sys.modules；算法文件在磁盘上被修改后（mtime 变化）
    自动作废缓存并重新加载，避免"文件已改好但服务器仍在用旧内容/跳过提示"
    这类困惑（正在运行的旧模块实例不受影响，重启后生效）。
    """
    name = _module_name(algorithm_id)
    path = ALGORITHMS_DIR / f"{algorithm_id}.py"
    if not path.is_file():
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None

    cached = sys.modules.get(name)
    if cached is not None and getattr(cached, "_mtime", None) == mtime:
        return cached

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    module._mtime = mtime
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _valid_algorithm_id(algorithm_id: str) -> bool:
    """算法 id 必须是合法标识符（防路径穿越）。"""
    return bool(algorithm_id) and algorithm_id.isidentifier()


def list_algorithms() -> list[dict]:
    """扫描 algorithms/*.py，返回所有算法元信息列表。"""
    algorithms = []
    for path in sorted(ALGORITHMS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module = _load_module(path.stem)
        meta = getattr(module, "ALGO_META", None)
        if meta is None or not callable(getattr(module, "run", None)):
            # 打印具体原因，避免"被跳过但不知道为什么"
            reason = "缺少 ALGO_META" if meta is None else "缺少 run() 函数"
            print(f"[registry] 跳过算法文件 {path.name}（{reason}，不符合平台规范）")
            continue
        if meta.get("id") != path.stem:
            print(f"[registry] 警告：{path.name} 的 ALGO_META.id 应为 '{path.stem}'，实际为 '{meta.get('id')}'")
        algorithms.append(meta)
    return algorithms


def load_algorithm(algorithm_id: str):
    """加载指定算法模块；不存在或不规范时返回 None。"""
    if not _valid_algorithm_id(algorithm_id):
        return None
    module = _load_module(algorithm_id)
    if module is None:
        return None
    meta = getattr(module, "ALGO_META", None)
    if meta is None or not callable(getattr(module, "run", None)):
        return None
    return module
