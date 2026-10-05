"""项目根路径推断（内核细模块，纯 stdlib）。

core/kernel/paths.py -> kernel -> core -> 项目根。
"""
from __future__ import annotations

import os


def project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
