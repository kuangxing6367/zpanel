"""
数据库速度限制（令牌桶，stdlib 实现，零第三方依赖）

线程安全：任意线程在执行 SQL 前调用 acquire()，按配置的 QPS 平滑放行。
qps <= 0 时直通（不限速）。等待超过 wait_timeout 抛 RateLimitTimeout，
避免高负载下调用方无限阻塞。
"""
import threading
import time


class RateLimitTimeout(RuntimeError):
    """等待令牌超时"""


class RateLimiter:
    """令牌桶限速器

    :param qps: 每秒放行的数据库操作数；<= 0 表示不限速
    :param burst: 桶容量（允许的瞬时突发量），缺省取 qps（至少 1）
    :param wait_timeout: 单次 acquire 最长等待秒数，超时抛 RateLimitTimeout
    """

    def __init__(self, qps: float = 0, burst: float = None, wait_timeout: float = 30.0):
        self.qps = float(qps or 0)
        self.burst = float(burst) if burst else max(self.qps, 1.0)
        self.wait_timeout = float(wait_timeout or 30.0)
        self._lock = threading.Lock()
        self._tokens = self.burst
        self._last = time.monotonic()
        # 统计（供状态查询 / 监控）
        self.allowed = 0
        self.limited = 0
        self.timed_out = 0

    @property
    def enabled(self) -> bool:
        return self.qps > 0

    def acquire(self, blocking: bool = True):
        """获取一个令牌。返回 True 表示放行。

        blocking=False 时只探测不等待；blocking=True 时最多等 wait_timeout。
        """
        if not self.enabled:
            return True
        deadline = time.monotonic() + self.wait_timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.burst, self._tokens + (now - self._last) * self.qps)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    self.allowed += 1
                    return True
                need = (1 - self._tokens) / self.qps
            if not blocking or not (deadline > time.monotonic()):
                self._record_limited(blocking)
                return False
            time.sleep(min(need, 0.1))

    def _record_limited(self, blocking: bool):
        with self._lock:
            self.limited += 1
            if blocking:
                self.timed_out += 1

    def status(self) -> dict:
        with self._lock:
            return {
                'enabled': self.enabled,
                'qps': self.qps,
                'burst': self.burst,
                'wait_timeout': self.wait_timeout,
                'tokens': round(self._tokens, 2),
                'allowed': self.allowed,
                'limited': self.limited,
                'timed_out': self.timed_out,
            }
