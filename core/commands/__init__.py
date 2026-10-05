"""命令机制包（core.commands）—— 内核薄分发原语。

提供与事件总线同形状的命令总线：``register(name, callable)`` + ``invoke(name, *args)``。
内核不内建路由 / 正则匹配 / 优先级 / 权限等策略——那些属于扩展层（router / 适配器）。
"""

from .bus import CommandBus

__all__ = ["CommandBus"]
