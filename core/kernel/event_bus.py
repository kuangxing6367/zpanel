"""事件总线（内核最小原语，全仓唯一实现）。

对外 API：
- subscribe(event_name, plugin_name, handler) / unsubscribe(...) / unsubscribe_plugin(...)
- aemit(event_name, payload)  异步发布，handler 支持 async def（直接 await）与普通 def（转线程）
- emit(event_name, payload)   同步桥接，供旧插件/非 loop 线程使用

订阅与退订线程安全（插件可能在 executor 线程中注册）。
``core.messaging.event_bus`` 对本模块做透明重导出，不另存实现。

线程模型关键点（优化点）：
- 实例在 Framework.__init__ 内构造，彼时正处于 amain 的运行事件循环中，
  故在构造时捕获该主循环引用 ``_loop``。
- emit() 在「非运行循环线程」（gRPC 线程、GraphQL 请求线程、to_thread 中跑的
  sync handler）调用时，不再每次 ``asyncio.run`` 起一个临时 loop（既低效，又会让
  handler 跑到错误的临时 loop 上导致发消息等主循环相关操作失效），而是用
  ``run_coroutine_threadsafe`` 把事件安全地调度回框架主循环。
- 完全无可用主循环时（独立脚本 / 部分测试）才退化为临时 loop，保持旧行为。
"""
import asyncio
import logging
import threading
from typing import Callable, Dict, List

logger = logging.getLogger('zernus')


class EventBus:
    """轻量级事件总线"""

    def __init__(self):
        self._subscribers: Dict[str, List[dict]] = {}
        self._lock = threading.Lock()
        # 捕获框架主事件循环（Framework 在 amain 的运行循环内构造本实例时，
        # 此处才有「正在运行」的 loop 可捕获）。仅当确有运行中的 loop 时才捕获，
        # 否则留 None（独立脚本/部分测试中构造 EventBus 时无运行 loop）。
        # 供非循环线程把事件调度回主循环，避免每次 emit 都新建/销毁临时事件循环。
        self._loop = None
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def subscribe(self, event_name: str, plugin_name: str, handler: Callable):
        """订阅事件（同一插件同一 handler 重复订阅自动去重）"""
        with self._lock:
            subs = self._subscribers.setdefault(event_name, [])
            for s in subs:
                if s['plugin_name'] == plugin_name and s['handler'] == handler:
                    return
            subs.append({
                'plugin_name': plugin_name,
                'handler': handler
            })
        logger.debug(f"事件订阅: [{plugin_name}] → {event_name}")

    def unsubscribe_plugin(self, plugin_name: str):
        """移除某插件的所有订阅"""
        with self._lock:
            for event_name in list(self._subscribers.keys()):
                self._subscribers[event_name] = [
                    s for s in self._subscribers[event_name]
                    if s['plugin_name'] != plugin_name
                ]
                if not self._subscribers[event_name]:
                    del self._subscribers[event_name]

    def unsubscribe(self, event_name: str, plugin_name: str, handler: Callable):
        """退订某插件在某个事件上的单个 handler（once/off 用）"""
        with self._lock:
            subs = self._subscribers.get(event_name)
            if not subs:
                return
            self._subscribers[event_name] = [
                s for s in subs
                if not (s['plugin_name'] == plugin_name and s['handler'] == handler)
            ]
            if not self._subscribers[event_name]:
                del self._subscribers[event_name]

    async def aemit(self, event_name: str, payload: dict = None) -> bool:
        """异步发布，返回 bool：任一 handler 返回 True 视为已处理"""
        with self._lock:
            subscribers = list(self._subscribers.get(event_name, []))
        if not subscribers:
            return False

        payload = payload or {}
        logger.debug(f"事件触发: {event_name} → {len(subscribers)} 个订阅者")

        handled = False
        for sub in subscribers:
            try:
                handler = sub['handler']
                if asyncio.iscoroutinefunction(handler):
                    result = await handler(payload)
                else:
                    result = await asyncio.to_thread(handler, payload)
                if result is True:
                    handled = True
            except Exception as e:
                logger.error(f"事件处理异常: [{sub['plugin_name']}] {event_name} - {e}")
        return handled

    def emit(self, event_name: str, payload: dict = None):
        """同步桥接发布。

        - 在运行中的事件循环内（register / async handler）→ fire-and-forget 挂任务。
        - 在其他线程（gRPC / GraphQL 线程、to_thread 中的 sync handler）→ 线程安全地
          调度到框架主循环，复用同一 loop，不每次新建临时 loop。
        - 完全无可用主循环（独立脚本 / 部分测试）→ 退化为临时 loop（保持旧行为）。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            try:
                loop.create_task(self.aemit(event_name, payload))
                return
            except RuntimeError:
                pass

        # 不在任何运行中的循环内：调度回框架主循环，避免临时 loop 开销与正确性风险。
        target = self._loop
        if target is not None and target.is_running() and not target.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(self.aemit(event_name, payload), target)
                return
            except RuntimeError:
                pass

        # 退化路径：无可用主循环（独立脚本 / 部分测试）。
        asyncio.run(self.aemit(event_name, payload))
