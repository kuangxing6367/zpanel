# -*- coding: utf-8 -*-
"""事件总线扩展（core/ctx 节点 18）

在已有 on/emit/aemit 之上补齐：once（一次性订阅，触发后即退订）、
off（退订指定 handler）、await_event（异步等待某事件到达，带超时）。
注意：命名 wait_for 已被 SessionMixin 占用（多轮会话等待用户消息），
故事件等待命名为 await_event 以避免 MRO 冲突。
"""
import asyncio
import logging

logger = logging.getLogger('zernus')


class EventBusExtMixin:
    """ctx 上的事件总线扩展能力。"""

    def once(self, event_name: str, handler):
        """一次性订阅：handler 被触发一次后自动退订。handler 签名 (payload)。"""
        name = self._plugin_name

        def _wrapper(payload):
            try:
                return handler(payload)
            finally:
                self._framework.event_bus.unsubscribe(event_name, name, _wrapper)

        self._framework.event_bus.subscribe(event_name, name, _wrapper)
        return _wrapper

    def off(self, event_name: str, handler=None):
        """退订。给定 handler 退订单个；handler=None 退订本插件在该事件上的全部订阅。"""
        bus = self._framework.event_bus
        if handler is not None:
            bus.unsubscribe(event_name, self._plugin_name, handler)
            return
        # 退订本插件在该事件上的所有 handler：EventBus 仅暴露按 handler 退订，
        # 这里借助「先取再退」——为安全起见仅退订由本插件注册者，需要遍历订阅表。
        subs = getattr(bus, '_subscribers', {}).get(event_name, [])
        for s in list(subs):
            if s['plugin_name'] == self._plugin_name:
                bus.unsubscribe(event_name, self._plugin_name, s['handler'])

    async def await_event(self, event_name: str, timeout: float = None):
        """异步等待某事件，返回 payload；超时抛出 asyncio.TimeoutError。"""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        name = self._plugin_name

        def _collector(payload):
            if not fut.done():
                fut.set_result(payload)
            return None

        self._framework.event_bus.subscribe(event_name, name, _collector)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._framework.event_bus.unsubscribe(event_name, name, _collector)
