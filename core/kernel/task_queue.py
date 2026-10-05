"""
内核任务队列（stdlib 实现，零第三方依赖）

通用后台任务列表：任意层（core / service / software）可提交 sync 或 async
可调用对象，由固定数量的 worker 线程按 FIFO 顺序执行；每条任务记录
id / 名称 / 状态 / 提交与完成时间 / 结果或异常，可通过 list_tasks()
获取快照（供终端 / WebUI 展示）。

async 任务优先投递到框架主事件循环（run_coroutine_threadsafe）；
未绑定 loop 时降级为 worker 线程内 asyncio.run。
"""
import asyncio
import logging
import threading
import time
import uuid
from concurrent.futures import Future

logger = logging.getLogger('zernus')

# 任务状态
PENDING = 'pending'
RUNNING = 'running'
DONE = 'done'
FAILED = 'failed'
CANCELLED = 'cancelled'


def _wants_task_id(fn) -> bool:
    """fn 是否声明了 task_id 形参（声明了才注入，不惊扰普通任务）。"""
    try:
        import inspect
        return 'task_id' in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


class TaskQueue:
    """内核任务队列（任务列表）

    :param workers: worker 线程数（默认 4）
    :param max_history: 保留的已完成任务记录条数（防止无限增长）
    """

    def __init__(self, workers: int = 4, max_history: int = 200, on_finish=None):
        self._workers_n = max(1, int(workers or 4))
        self._max_history = max(10, int(max_history or 200))
        # on_finish(record)：任务终态回调（done/failed/cancelled）——
        # 引擎用它把任务历史落库，重启后任务中心仍有账可查
        self._on_finish = on_finish
        self._queue = []
        self._cv = threading.Condition()
        self._tasks = {}       # task_id -> record（dict，含 Future）
        self._order = []       # 提交顺序的 task_id 列表
        self._loop = None      # 主事件循环（async 任务投递用）
        self._workers = []
        self._running = False
        self._seq = 0

    # ── 生命周期 ──

    def start(self, loop=None):
        if loop is not None:
            self._loop = loop
        self._running = True
        for i in range(self._workers_n):
            t = threading.Thread(
                target=self._worker, name=f'zctask-{i}', daemon=True)
            t.start()
            self._workers.append(t)
        logger.info(f"任务队列已启动（{self._workers_n} 个 worker）")

    def stop(self, timeout: float = 5.0):
        self._running = False
        with self._cv:
            self._cv.notify_all()
        deadline = time.time() + timeout
        for t in self._workers:
            t.join(max(0.0, deadline - time.time()))
        self._workers = []
        logger.info("任务队列已停止")

    # ── 提交 ──

    def submit(self, fn, *args, name: str = None, meta: dict = None, **kwargs) -> str:
        """提交任务，返回 task_id。fn 可为 sync 可调用对象或协程函数。

        meta：随任务展示的上下文（如 {'kind': 'svc.install', 'node': ...}）。
        执行时若 fn 声明了 ``task_id`` 形参，会自动注入任务 id，
        便于任务内部用 self.log(task_id, ...) 写进度。
        """
        if not callable(fn):
            raise TypeError("submit 需要 callable")
        with self._cv:
            self._seq += 1
            task_id = f"task_{self._seq}_{uuid.uuid4().hex[:8]}"
            record = {
                'id': task_id,
                'name': name or getattr(fn, '__name__', 'task'),
                'state': PENDING,
                'submitted_at': time.time(),
                'started_at': None,
                'finished_at': None,
                'error': None,
                'result': None,
                'meta': dict(meta or {}),
                'log': [],              # 进度日志（有界，新行在后）
            }
            self._tasks[task_id] = record
            self._order.append(task_id)
            self._cv.notify()
        fut = Future()
        record['future'] = fut
        with self._cv:
            self._queue.append((task_id, fn, args, kwargs, fut))
            self._cv.notify()
        return task_id

    def submit_async(self, coro, name: str = None) -> str:
        """提交协程任务。有主循环时投递到主循环执行，否则 worker 内 asyncio.run。"""

        async def _runner():
            return await coro

        return self.submit(
            _runner, name=name or getattr(coro, '__name__', 'async_task'))

    # ── 查询 ──

    def list_tasks(self, state: str = None, limit: int = 100) -> list:
        """任务列表快照（按提交时间倒序）。state 可过滤，limit 限制条数。"""
        with self._cv:
            ids = list(reversed(self._order))
        out = []
        for tid in ids:
            rec = self._tasks.get(tid)
            if rec is None:
                continue
            if state and rec['state'] != state:
                continue
            out.append(self._snapshot(rec))
            if len(out) >= limit:
                break
        return out

    def get_task(self, task_id: str) -> dict:
        rec = self._tasks.get(task_id)
        return self._snapshot(rec) if rec else None

    def log(self, task_id: str, text: str) -> None:
        """任务执行中追加一行进度日志（有界；不存在的任务静默忽略）。"""
        rec = self._tasks.get(task_id)
        if rec is None:
            return
        entry = f"[{time.strftime('%H:%M:%S')}] {text}"
        with self._cv:
            log = rec.setdefault('log', [])
            log.append(entry)
            if len(log) > 400:
                del log[:len(log) - 400]

    def update(self, task_id: str, **fields) -> None:
        """任务执行中更新展示字段（如 progress）。id/future 不许改。"""
        rec = self._tasks.get(task_id)
        if rec is None:
            return
        with self._cv:
            for k, v in fields.items():
                if k in ('id', 'future', '_'):
                    continue
                rec[k] = v

    def cancel(self, task_id: str) -> dict:
        """取消任务。**只有还在排队的能取消**——执行中的是进程/子进程，
        Python 线程不能强杀，如实拒绝而不是假装取消了。"""
        with self._cv:
            rec = self._tasks.get(task_id)
            if rec is None:
                return {"ok": False, "error": "任务不存在"}
            if rec['state'] == PENDING:
                rec['state'] = CANCELLED
                rec['finished_at'] = time.time()
                # 从队列摘掉，worker 就不会捡起来
                self._queue[:] = [q for q in self._queue if q[0] != task_id]
                fut = rec.get('future')
                if fut is not None and not fut.done():
                    fut.set_exception(RuntimeError("任务已取消"))
                return {"ok": True, "state": CANCELLED}
            return {"ok": False,
                    "error": f"任务已在执行中（{rec['state']}），无法取消 ——"
                             f"执行型任务不支持中断，等它自然结束"}

    def stats(self) -> dict:
        with self._cv:
            counts = {}
            for rec in self._tasks.values():
                counts[rec['state']] = counts.get(rec['state'], 0) + 1
        return {
            'workers': self._workers_n,
            'running': self._running,
            'queued': len(self._queue) if hasattr(self, '_queue') else 0,
            **counts,
        }

    @staticmethod
    def _snapshot(rec: dict) -> dict:
        return {k: v for k, v in rec.items() if k != 'future' and not k.startswith('_')}

    # ── worker ──

    def _worker(self):
        while True:
            with self._cv:
                while self._running and not self._queue:
                    self._cv.wait(timeout=1.0)
                if not self._running and not self._queue:
                    return
                item = self._queue.pop(0)
            task_id, fn, args, kwargs, fut = item
            rec = self._tasks.get(task_id)
            if rec is None:
                continue
            if rec['state'] == CANCELLED:      # 排队时被取消 → 不执行
                continue
            self._run_one(rec, fn, args, kwargs, fut)

    def _run_one(self, rec: dict, fn, args, kwargs, fut: Future):
        rec['state'] = RUNNING
        rec['started_at'] = time.time()
        try:
            if asyncio.iscoroutinefunction(fn):
                if self._loop is not None and self._loop.is_running():
                    coro_fut = asyncio.run_coroutine_threadsafe(
                        fn(*args, **kwargs), self._loop)
                    result = coro_fut.result()
                else:
                    result = asyncio.run(fn(*args, **kwargs))
            else:
                if _wants_task_id(fn):
                    kwargs = {**kwargs, 'task_id': rec['id']}
                result = fn(*args, **kwargs)
            rec['result'] = result
            rec['state'] = DONE
            fut.set_result(result)
        except Exception as e:
            rec['error'] = f"{type(e).__name__}: {e}"
            rec['state'] = FAILED
            fut.set_exception(e)
            logger.warning(f"任务 [{rec['name']}] 执行失败: {e}")
        finally:
            rec['finished_at'] = time.time()
            try:
                if self._on_finish is not None:
                    self._on_finish(self._snapshot(rec))
            except Exception as e:
                logger.debug("[task] 历史落库失败: %s", e)
            self._trim()

    def _trim(self):
        """裁剪已完成的历史任务记录，保持列表有界"""
        done_states = {DONE, FAILED, CANCELLED}
        done_ids = [tid for tid in self._order
                    if (rec := self._tasks.get(tid)) and rec['state'] in done_states]
        overflow = len(done_ids) - self._max_history
        for tid in done_ids[:max(0, overflow)]:
            self._tasks.pop(tid, None)
            try:
                self._order.remove(tid)
            except ValueError:
                pass
