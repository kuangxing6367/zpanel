"""日志初始化（内核细模块）。

与框架解耦：只依赖标准库；需要桥接 WebUI 日志时，由调用方把对应 handler
通过 ``extra_handlers`` 传入（框架侧负责提供，内核不反向依赖框架）。

约定：
- 控制台 + 滚动文件（data/logs/zernus.log，10MB×5）双输出。
- 第三方库（按需在扩展侧降噪）降噪到 WARNING。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from typing import List, Optional


def setup_logging(config: dict, project_root: str,
                  extra_handlers: Optional[List[logging.Handler]] = None) -> str:
    """配置根日志，返回日志文件绝对路径。"""
    log_level = config.get("log", {}).get("level", "INFO")

    log_file = config.get("log", {}).get("file") or os.path.join("data", "logs", "zernus.log")
    if not os.path.isabs(log_file):
        log_file = os.path.join(project_root, log_file)
    log_file = os.path.abspath(log_file)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S"))

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5,
        encoding="utf-8", delay=True,
    )
    file_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s [%(name)s] %(message)s"))

    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    root.addHandler(console)
    root.addHandler(file_handler)
    for h in (extra_handlers or []):
        root.addHandler(h)

    return log_file
