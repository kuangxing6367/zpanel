# -*- coding: utf-8 -*-
"""进程内缓存（core/ctx 节点 19）

提供 ctx.cache_get/cache_set/cache_delete，底层是挂在 framework 上的
单个 CacheStore（全部插件共享命名空间，键建议带插件名前缀）。
适合插件缓存远程拉取结果、限流计数等；非持久化，重启即失。
"""
import logging

logger = logging.getLogger('zernus')


class CacheMixin:
    """ctx 上的进程内 TTL 缓存。"""

    def _cache_store(self):
        store = getattr(self._framework, '_plugin_cache_store', None)
        if store is None:
            from core.cache import CacheStore
            store = CacheStore()
            self._framework._plugin_cache_store = store
        return store

    def cache_set(self, key, value, ttl: float = None):
        """写入缓存；ttl 秒，默认 300。键建议加插件名前缀避免碰撞。"""
        self._cache_store().set(key, value, ttl)

    def cache_get(self, key, default=None):
        """读取缓存，过期/缺失返回 default。"""
        return self._cache_store().get(key, default)

    def cache_delete(self, key):
        """删除缓存键。"""
        self._cache_store().delete(key)
