"""pathsafe —— 安全路径处理（机制包）。

框架提供的一等公民程序化接口：拼接用户可控路径时防止目录穿越
（../../etc/passwd 之类），插件处理上传/下载/配置路径的标配。

稳定 API 表面：
    safe_join(base, *parts) -> str       拼接并校验，越界抛 PathTraversalError
    is_within(base, path) -> bool        判断 path 是否在 base 内
    PathTraversalError(ValueError)       越界异常

设计约定：基于 os.path.abspath + commonpath，跨平台；base 不存在也可判断。
"""
from __future__ import annotations

import os


class PathTraversalError(ValueError):
    """路径越界（目标不在基准目录内）。"""


def is_within(base: str, path: str) -> bool:
    """判断 path 是否位于 base 目录内（含 base 自身）。"""
    base_abs = os.path.abspath(base)
    path_abs = os.path.abspath(path)
    try:
        return os.path.commonpath([base_abs, path_abs]) == base_abs
    except ValueError:
        # 不同盘符（Windows）等
        return False


def safe_join(base: str, *parts: str) -> str:
    """在 base 下安全拼接 parts；结果越界则抛 PathTraversalError。"""
    joined = os.path.abspath(os.path.join(base, *parts))
    if not is_within(base, joined):
        raise PathTraversalError(f"路径越界: {joined!r} 不在 {os.path.abspath(base)!r} 内")
    return joined
