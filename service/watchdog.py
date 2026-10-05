"""服务级看门狗：监控内存占用，超限时记录并建议回收。

对应手写笔记：
- 服务级看门狗：自身服务内存处理、zkg 拉起的服务。
- 软件级看门狗：内存。
"""
import logging
import threading
import time

logger = logging.getLogger('zernus')


class Watchdog:
    """内存看门狗（服务级 / 软件级共用）。

    周期性采样本进程常驻内存，超过上限时在日志告警（具体回收策略由调用方决定）。
    psutil 缺失时自动降级为无操作（不报错）。
    """

    def __init__(self, limit_mb: int = 64, interval: float = 30.0):
        self.limit_mb = int(limit_mb)
        self.interval = float(interval)
        self._stop = threading.Event()
        self._thread = None

    def _mem_mb(self) -> float:
        try:
            import psutil
            return psutil.Process().memory_info().rss / 1024 / 1024
        except Exception:
            return 0.0

    def _loop(self):
        while not self._stop.is_set():
            mb = self._mem_mb()
            if self.limit_mb and mb > self.limit_mb:
                logger.warning(
                    f"[watchdog] 内存 {mb:.1f}MB 超过上限 {self.limit_mb}MB，建议回收")
            self._stop.wait(self.interval)

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
