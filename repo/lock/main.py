"""lock —— 互斥锁抽象（机制包）。

框架提供的一等公民程序化接口：多线程/多进程并发写共享资源的标配，
不再各自手搓 threading.Lock 字典或裸文件锁。

稳定 API 表面：
    named(name) -> threading.Lock       进程内命名锁单例（同名同锁）
    FileLock(path, *, timeout=10.0)     跨进程文件锁（Windows/POSIX 自适配）
        .acquire() / .release() / with 语法；拿不到锁抛 LockTimeout

设计约定：
- FileLock 的锁文件自动落目录（不自动清理，文件本身即锁状态）；
- 同进程同路径可重入（进程内注册表计数，Windows/POSIX 行为一致）；
  跨进程互斥由 OS 文件锁保证；
- 阻塞等待受 timeout 约束，超时抛 LockTimeout 而非死等。
"""
from __future__ import annotations

import os
import threading
import time

try:  # Windows
    import msvcrt
    _WIN = True
except ImportError:  # POSIX
    import fcntl
    _WIN = False

_named_locks: dict = {}
_named_guard = threading.Lock()

# 跨进程文件锁的进程内注册表：abspath -> {"fh", "depth", "tlock"}
_fl_registry: dict = {}
_fl_guard = threading.Lock()


def named(name: str) -> threading.Lock:
    """进程内命名锁单例：同名返回同一把锁（线程安全地创建）。"""
    with _named_guard:
        lk = _named_locks.get(name)
        if lk is None:
            lk = threading.Lock()
            _named_locks[name] = lk
        return lk


class LockTimeout(Exception):
    """文件锁等待超时。"""


class FileLock:
    """跨进程文件锁；同进程同路径重入（可重入计数 + 线程互斥）。"""

    def __init__(self, path: str, *, timeout: float = 10.0):
        self.path = os.path.abspath(path)
        self.timeout = timeout
        self._local_depth = 0

    # -- 内部：OS 层加/解锁（仅首次持有者调用） ------------------

    @staticmethod
    def _os_lock(fh) -> None:
        if _WIN:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _os_unlock(fh) -> None:
        if _WIN:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    # -- 公开 API ----------------------------------------------

    def acquire(self) -> "FileLock":
        deadline = time.time() + self.timeout
        while True:
            with _fl_guard:
                entry = _fl_registry.get(self.path)
                if entry is not None:
                    # 同进程已有持有者：线程间互斥靠 tlock（RLock，同线程可重入）
                    if entry["tlock"].acquire(timeout=0.05):
                        entry["depth"] += 1
                        self._local_depth += 1
                        return self
                else:
                    os.makedirs(os.path.dirname(self.path), exist_ok=True)
                    fh = open(self.path, "a+b")
                    try:
                        self._os_lock(fh)
                    except OSError:
                        fh.close()
                    else:
                        entry = {"fh": fh, "depth": 1,
                                 "tlock": threading.RLock()}
                        entry["tlock"].acquire()
                        _fl_registry[self.path] = entry
                        self._local_depth += 1
                        return self
            if time.time() >= deadline:
                raise LockTimeout(f"文件锁等待超时 {self.timeout}s: {self.path}")
            time.sleep(0.05)

    def release(self):
        with _fl_guard:
            entry = _fl_registry.get(self.path)
            if entry is None or self._local_depth == 0:
                return
            self._local_depth -= 1
            entry["depth"] -= 1
            if entry["depth"] <= 0:
                try:
                    self._os_unlock(entry["fh"])
                finally:
                    entry["fh"].close()
                    _fl_registry.pop(self.path, None)
            entry["tlock"].release()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False
