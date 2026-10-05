"""扫描插件 / 包目录，收集 manifest。"""

from __future__ import annotations

import os
from typing import List

from .manifest import Manifest


def scan_dir(root: str, source_id: str = "local") -> List[Manifest]:
    """递归扫描 root，凡含 ``manifest.toml`` 的目录都加载为一个包。

    返回 Manifest 列表；每个 Manifest.path 设为该包根目录。
    """
    manifests: List[Manifest] = []
    if not os.path.isdir(root):
        return manifests
    for dirpath, _dirs, files in os.walk(root):
        if "manifest.toml" in files:
            try:
                m = Manifest.load(os.path.join(dirpath, "manifest.toml"))
                m.path = dirpath
                m.source_id = source_id
                manifests.append(m)
            except Exception as e:  # 单个坏包不应拖垮扫描
                print(f"[pkg] 跳过无法解析的 manifest {dirpath}: {e}")
    return manifests
