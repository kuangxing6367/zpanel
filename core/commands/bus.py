"""命令总线（内核细模块，薄分发原语）。

与 ``event_bus`` 同形状：``register(name, callable)`` + ``invoke(name, *args)``。
内核只提供「注册 name→程序」与「按 name 调用」两个原语，

**不内建**路由拓扑 / 正则 pattern 匹配 / 优先级裁决 / 权限门控 /
聊天消息→命令的翻译——这些策略由扩展层（router / 适配器）决定，不属于内核机制。

消息如何被翻译成命令、命中哪个 pattern、按什么优先级裁决、是否门控权限，
全部是应用/扩展策略，本模块一概不管。
"""

import asyncio
import logging
import threading
from typing import Callable, Dict, List, Optional

logger = logging.getLogger('zernus')


class CommandBus:
    """轻量级命令总线：name → callable 注册表 + 调用分发（线程安全）"""

    def __init__(self):
        self._registry: Dict[str, dict] = {}
        self._lock = threading.Lock()

    # ---- 注册 / 注销 ----

    def register(self, name: str, func: Callable, owner: str = None):
        """注册命令：name → callable（同名同 owner 重复注册自动覆盖）"""
        name = self._normalize(name)
        if not callable(func):
            raise TypeError(f"command '{name}' 的 handler 不可调用")
        with self._lock:
            self._registry[name] = {'owner': owner, 'func': func}
        logger.debug(f"命令注册: [{owner}] → {name}")

    def unregister(self, name: str):
        """注销单个命令"""
        name = self._normalize(name)
        with self._lock:
            self._registry.pop(name, None)

    def unregister_owner(self, owner: str):
        """移除某 owner 注册的全部命令（插件卸载 / 重载清理用）"""
        with self._lock:
            for n in [k for k, v in self._registry.items() if v['owner'] == owner]:
                del self._registry[n]

    def exists(self, name: str) -> bool:
        name = self._normalize(name)
        with self._lock:
            return name in self._registry

    def list(self) -> Dict[str, Optional[str]]:
        """返回 name → owner 映射的只读副本"""
        with self._lock:
            return {n: v['owner'] for n, v in self._registry.items()}

    # ---- 调用 ----

    async def ainvoke(self, name: str, *args, **kwargs):
        """异步调用，返回 handler 结果（支持 async def / 普通 def 转线程）"""
        name = self._normalize(name)
        with self._lock:
            entry = self._registry.get(name)
        if entry is None:
            raise KeyError(f"未注册的命令: {name}")
        func = entry['func']
        try:
            if asyncio.iscoroutinefunction(func):
                return await func(*args, **kwargs)
            return await asyncio.to_thread(func, *args, **kwargs)
        except Exception as e:
            logger.error(f"命令执行异常: [{entry['owner']}] {name} - {e}")
            raise

    def invoke(self, name: str, *args, **kwargs):
        """同步桥接调用（同 event_bus.emit 的桥接策略：有运行 loop 则 fire-and-forget）"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            try:
                loop.create_task(self.ainvoke(name, *args, **kwargs))
                return None
            except RuntimeError:
                pass
        return asyncio.run(self.ainvoke(name, *args, **kwargs))

    # ---- 内部 ----

    @staticmethod
    def _normalize(name: str) -> str:
        return str(name).strip().lower()
