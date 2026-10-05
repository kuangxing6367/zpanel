# -*- coding: utf-8 -*-
"""极简依赖注入容器（内核机制）。

被 ctx.provide/ctx.inject 复用；单个 DIContainer 挂在 framework 上，
供插件间解耦共享对象（如共享客户端、模型句柄、连接池）。
与 service.register 的区别：DI 是「按名字取对象」，服务注册表是
「按名字取核心能力」；DI 更轻量、面向插件间协作。
零第三方依赖。
"""
import threading
from typing import Callable, Dict, Optional

_MISSING = object()


class DIContainer:
    """名字 -> 工厂/实例 的依赖容器。"""

    def __init__(self):
        self._singletons: Dict[str, object] = {}
        self._factories: Dict[str, Callable[[], object]] = {}
        self._lock = threading.RLock()

    def provide(self, name: str, obj=None, factory: Callable[[], object] = None):
        """注册一个依赖。

        - provide(name, obj)：直接注册单例
        - provide(name, factory=callable)：注册工厂，首次 inject 时惰性构造并缓存
        """
        if obj is None and factory is None:
            raise ValueError("provide 必须给定 obj 或 factory 之一")
        with self._lock:
            if factory is not None:
                self._factories[name] = factory
                self._singletons.pop(name, None)
            else:
                self._singletons[name] = obj
                self._factories.pop(name, None)

    def inject(self, name: str, default=_MISSING):
        """取出依赖（工厂型首次调用时构造并缓存为单例）。"""
        with self._lock:
            if name in self._singletons:
                return self._singletons[name]
            factory = self._factories.get(name)
            if factory is not None:
                inst = factory()
                self._singletons[name] = inst
                return inst
        if default is _MISSING:
            raise KeyError(f"未注册的依赖: {name}")
        return default

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._singletons or name in self._factories

    def unprovide(self, name: str):
        with self._lock:
            self._singletons.pop(name, None)
            self._factories.pop(name, None)
