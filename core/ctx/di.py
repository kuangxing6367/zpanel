# -*- coding: utf-8 -*-
"""依赖注入（core/ctx 节点 20）

提供 ctx.provide/ctx.inject，让插件间解耦共享对象（共享客户端、
模型句柄、连接池等）。底层是挂在 framework 上的单个 DIContainer。
与 service.register 区别：DI 是「按名取对象」，服务注册表是
「按名取核心能力」；DI 更轻量、面向插件间协作。
"""
import logging

logger = logging.getLogger('zernus')


class DIMixin:
    """ctx 上的依赖注入。"""

    def _di_container(self):
        cont = getattr(self._framework, '_di_container', None)
        if cont is None:
            from core.di import DIContainer
            cont = DIContainer()
            self._framework._di_container = cont
        return cont

    def provide(self, name: str, obj=None, factory=None):
        """注册依赖：provide(name, obj) 直接单例；provide(name, factory=callable) 惰性构造。"""
        self._di_container().provide(name, obj=obj, factory=factory)

    def inject(self, name: str, default=None):
        """取出依赖；未注册且无 default 时抛 KeyError。"""
        cont = self._di_container()
        if default is None:
            return cont.inject(name)
        try:
            return cont.inject(name)
        except KeyError:
            return default
