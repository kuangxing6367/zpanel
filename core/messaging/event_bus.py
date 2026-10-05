"""事件总线（透明重导出）。

单一实现位于 ``core.kernel.event_bus``（内核最小原语），本模块仅做透明重导出，
以保持历史引用 ``from core.messaging.event_bus import EventBus`` 可用。

此前本模块与 ``core.kernel.event_bus`` 各存一份近似实现：本模块缺 ``unsubscribe()``，
而 ``core.ctx.EventBusExtMixin`` 的 once/off/await_event 依赖该方法，运行时会
``AttributeError``（单测因 wire 的是 kernel 版而被掩盖）。收敛为单一实现后不复存在。
"""
from core.kernel.event_bus import EventBus

__all__ = ["EventBus"]
