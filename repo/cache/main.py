"""cache —— TTL + LRU 内存缓存（机制包）。

框架提供的一等公民程序化接口：热数据缓存、限频打点、
重复计算结果复用，不必各自手搓 dict + 过期判断。

稳定 API 表面：
    Cache(maxsize=128, ttl=None)
    .get(key, default=None) / .set(key, value, ttl=None) / .delete(key)
    .clear() / .__len__() / .stats()
    cached(cache, key_fn=None, ttl=None) -> 装饰器

设计约定：
- 命中即 move_to_end，超 maxsize 淘汰最久未用（LRU）；
- ttl 为秒，set 时可覆盖；过期项在 get 时惰性删除；
- 线程安全（RLock）。
"""
from __future__ import annotations

import functools
import threading
import time
from collections import OrderedDict


class Cache:
    def __init__(self, maxsize: int = 128, ttl: float | None = None):
        if maxsize < 1:
            raise ValueError("maxsize 必须 >= 1")
        self.maxsize = maxsize
        self.ttl = ttl
        self._data: OrderedDict = OrderedDict()  # key -> (value, expire_at|None)
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0

    def get(self, key, default=None):
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self._misses += 1
                return default
            value, exp = item
            if exp is not None and time.monotonic() > exp:
                del self._data[key]
                self._misses += 1
                return default
            self._data.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key, value, ttl: float | None = None):
        t = self.ttl if ttl is None else ttl
        exp = None if t is None else time.monotonic() + t
        with self._lock:
            self._data[key] = (value, exp)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def delete(self, key) -> bool:
        with self._lock:
            return self._data.pop(key, None) is not None

    def clear(self):
        with self._lock:
            self._data.clear()

    def stats(self) -> dict:
        with self._lock:
            return {
                "size": len(self._data),
                "maxsize": self.maxsize,
                "hits": self._hits,
                "misses": self._misses,
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


def cached(cache: Cache, key_fn=None, ttl: float | None = None):
    """装饰器：按 cache 缓存函数返回值。key_fn(*args, **kw) 自定义键。"""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kw):
            key = key_fn(*args, **kw) if key_fn else (args, tuple(sorted(kw.items())))
            sentinel = object()
            v = cache.get(key, sentinel)
            if v is not sentinel:
                return v
            v = fn(*args, **kw)
            cache.set(key, v, ttl=ttl)
            return v
        wrapper.cache = cache
        return wrapper
    return deco
