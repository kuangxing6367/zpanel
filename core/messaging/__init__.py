"""消息事件通讯模块

集中框架的消息 / 事件通讯能力（实现原位于 framework.messaging，已迁至 core.messaging）：
- event:        消息事件模型与解析
- event_bus:    事件发布订阅总线
- router:       消息路由分发器

协议抽象（ProtocolAdapter / ServiceRegistry / ActionProxy）的真实实现位于
core.adapters.protocol，此处仅为向后兼容再导出。
"""

from core.adapters.protocol import ProtocolAdapter, ServiceRegistry, ActionProxy
from .event import Event
from .event_bus import EventBus
from .router import MessageRouter

__all__ = [
    'Event',
    'EventBus',
    'ProtocolAdapter',
    'ServiceRegistry',
    'ActionProxy',
    'MessageRouter',
]
