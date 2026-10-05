"""内置默认源配置。

类 apt：框架出厂自带一个「官方镜像源」（zkg.zgric.top），
同时本地仓库 ./repo 作为离线兜底。用户可在 config 的 pkg.sources
里追加 / 覆盖（例：加私有源、换镜像）。

注意：这里只描述「源的位置」，不内联任何凭据。远程镜像默认公开只读。
"""

from __future__ import annotations

from pathlib import Path

# service/zkg/defaults.py -> parents[0]=zkg, [1]=service, [2]=项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_SOURCES = [
    {
        "id": "local",
        "type": "local",
        "path": str(PROJECT_ROOT / "repo"),
        "enabled": True,
        "auth": "none",
        "kind": "official",
    },
    {
        "id": "zkg",
        "type": "http",
        "url": "https://zkg.zgric.top/zeronus",
        "enabled": True,
        "auth": "none",
        "kind": "official",
    },
]


def get_default_sources() -> list:
    """返回默认源列表的深拷贝，避免调用方误改内置配置。"""
    import copy

    return copy.deepcopy(DEFAULT_SOURCES)
