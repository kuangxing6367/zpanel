"""core.kernel —— 内核细模块集合。

每个模块单一职责、各自暴露清晰 API，互不耦合，并尽量零框架/第三方依赖：

- ``event_bus``     事件总线（register/subscribe + emit）
- ``banner``        启动横幅渲染（纯函数）
- ``logging_setup`` 日志初始化（可注入额外 handler，不反向依赖框架）
- ``data_dirs``     数据目录迁移（纯 stdlib）
- ``stats_writer``  异步统计批量写库器（注入 db + executor）
"""
from .event_bus import EventBus
from .banner import read_version, render_banner, emit_banner
from .logging_setup import setup_logging
from .data_dirs import migrate_legacy_data_dirs
from .stats_writer import AsyncStatsWriter

__all__ = [
    "EventBus",
    "read_version",
    "render_banner",
    "emit_banner",
    "setup_logging",
    "migrate_legacy_data_dirs",
    "AsyncStatsWriter",
]
