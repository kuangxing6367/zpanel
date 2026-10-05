# -*- coding: utf-8 -*-
"""进程内 TTL 缓存（内核机制）。

被 ctx.cache_get/cache_set 复用；单个 CacheStore 挂在 framework 上，
供全部插件共享一套命名空间（键建议加插件名前缀以避免碰撞）。
零第三方依赖，纯标准库实现。
"""
import threading
import time


class CacheStore:
    """带过期时间的简单内存缓存。"""

    def __init__(self, default_ttl: float = 300.0):
        self._default_ttl = default_ttl
        self._data: dict = {}
        self._lock = threading.Lock()

    def set(self, key, value, ttl: float = None):
        ttl = self._default_ttl if ttl is None else ttl
        with self._lock:
            self._data[key] = (value, time.time() + ttl)

    def get(self, key, default=None):
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return default
            value, expire = item
            if expire <= time.time():
                self._data.pop(key, None)
                return default
            return value

    def delete(self, key):
        with self._lock:
            self._data.pop(key, None)

    def clear(self):
        with self._lock:
            self._data.clear()

    def __contains__(self, key):
        return self.get(key, _MISSING) is not _MISSING


_MISSING = object()
