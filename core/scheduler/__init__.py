"""
定时任务调度器（stdlib 实现，零第三方依赖）

基于 asyncio 的自旋 tick：每 5 秒检查一次，匹配 5 字段 cron
（minute hour day month day_of_week），命中且本分钟尚未触发则执行。
day_of_week 采用 Python datetime.weekday() 约定（0=周一 … 6=周日），
与 APScheduler CronTrigger 默认约定一致。

这是内核机制层，不绑定任何第三方调度栈；对外只暴露薄薄的
add_plugin_task / remove_plugin_task(s) / remove_job / pause_task /
resume_task / get_jobs，与具体 Web/协议扩展解耦。
"""
import asyncio
import logging
from datetime import datetime

from core.hooks import HookPoints

logger = logging.getLogger('zernus')


def _field_match(value: int, expr, min_v: int, max_v: int) -> bool:
    """匹配单个 cron 字段。支持 * / ? / , / - / /step 组合。"""
    expr = (expr or '*').strip()
    if expr in ('*', '?'):
        return True
    for part in expr.split(','):
        part = part.strip()
        if not part:
            continue
        step = 1
        if '/' in part:
            rng, step_s = part.split('/', 1)
            step = int(step_s)
            part = rng
        if part in ('*', '?'):
            lo, hi = min_v, max_v
        elif '-' in part:
            lo_s, hi_s = part.split('-', 1)
            lo, hi = int(lo_s), int(hi_s)
        else:
            lo = hi = int(part)
        if lo <= value <= hi and (value - lo) % step == 0:
            return True
    return False


class TaskScheduler:
    """内核定时任务调度器（stdlib 实现）"""

    def __init__(self, framework):
        self.framework = framework
        self._jobs = {}            # task_id -> job info（含解析后的 cron 字段）
        self._running = False
        self._task = None

    # ── 生命周期 ──

    def start(self, loop=None):
        """启动调度器（绑定主事件循环）。loop 缺省取 framework.loop。"""
        if loop is None:
            loop = getattr(self.framework, 'loop', None)
        if loop is None:
            loop = asyncio.get_event_loop()
        self._running = True
        self._task = asyncio.ensure_future(self._tick_loop(), loop=loop)
        logger.info("定时任务调度器已启动（stdlib cron）")

    def stop(self):
        """停止调度器"""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        logger.info("定时任务调度器已停止")

    # ── 任务注册 ──

    def add_plugin_task(self, task_info: dict):
        """
        添加插件定时任务
        :param task_info: {id, plugin_name, cron_expression, handler,
                           handler_name, description}
        """
        plugin_name = task_info['plugin_name']
        jid = task_info.get('id') or task_info.get('handler_name') \
            or (task_info['handler'].__name__ if callable(task_info.get('handler')) else None)
        if jid is None:
            logger.error(f"添加任务失败: 缺少 id/handler_name [{plugin_name}]")
            return
        task_id = f"plugin_{plugin_name}_{jid}"
        cron_expr = task_info['cron_expression']

        parts = cron_expr.strip().split()
        if len(parts) != 5:
            logger.error(f"cron表达式格式错误: {cron_expr}")
            return
        try:
            # 仅做基础可解析性校验；真实匹配在 tick 时进行
            for p in parts:
                _field_match(0, p, 0, 59)
        except (ValueError, TypeError) as e:
            logger.error(f"cron表达式解析失败: {cron_expr} - {e}")
            return

        handler = task_info.get('handler')
        handler_name = task_info.get('handler_name') or (
            handler.__name__ if callable(handler) else None
        )
        if not callable(handler):
            module = self.framework.plugin_loader.get_plugin_module(plugin_name)
            if module is None:
                logger.error(f"添加任务失败: 插件 [{plugin_name}] 未加载")
                return
            handler = getattr(module, handler_name, None)
        if handler is None or not callable(handler):
            logger.error(f"添加任务失败: 函数 {handler_name} 在 [{plugin_name}] 中不存在或不可调用")
            return

        self._jobs[task_id] = {
            'plugin_name': plugin_name,
            'handler': handler,
            'handler_name': handler_name,
            'description': task_info.get('description', ''),
            'cron': parts,
            'paused': False,
            'last_fire': None,
        }
        logger.info(f"定时任务已注册: [{plugin_name}] {cron_expr} → {handler_name}")

    async def _run_job(self, handler, plugin_name: str):
        """执行任务：async handler 直接 await，sync handler 转线程"""
        handler_name = getattr(handler, '__name__', 'job')
        hooks = self.framework.hooks
        await hooks.trigger_async(
            HookPoints.CRON_TASK_TRIGGER_BEFORE, plugin_name=plugin_name, handler_name=handler_name
        )
        try:
            logger.debug(f"定时任务执行: [{plugin_name}] {handler_name}")
            if asyncio.iscoroutinefunction(handler):
                await handler()
            else:
                await asyncio.to_thread(handler)
            await asyncio.to_thread(
                self._update_task_status, plugin_name, handler_name, 'success'
            )
            await hooks.trigger_async(
                HookPoints.CRON_TASK_TRIGGER_AFTER, plugin_name=plugin_name,
                handler_name=handler_name, status='success'
            )
        except Exception as e:
            logger.error(f"定时任务异常: [{plugin_name}] {handler_name} - {e}")
            try:
                await asyncio.to_thread(
                    self._update_task_status, plugin_name, handler_name, 'error'
                )
            except Exception:
                pass
            await hooks.trigger_async(
                HookPoints.CRON_TASK_TRIGGER_AFTER, plugin_name=plugin_name,
                handler_name=handler_name, status='error'
            )

    def _update_task_status(self, plugin_name: str, handler_name: str, status: str):
        """更新任务执行状态（在线程中执行，不阻塞事件循环）"""
        try:
            now = datetime.now()
            self.framework.db.execute(
                "UPDATE tasks SET last_run_at=%s, run_count=run_count+1, last_status=%s "
                "WHERE plugin_name=%s AND handler=%s",
                (now, status, plugin_name, handler_name)
            )
        except Exception as e:
            logger.error(f"更新任务状态失败: {e}")

    # ── 任务管理 ──

    def remove_plugin_tasks(self, plugin_name: str):
        """移除某插件的所有任务"""
        for tid in [t for t, info in self._jobs.items() if info['plugin_name'] == plugin_name]:
            self._jobs.pop(tid, None)

    def remove_job(self, task_key: str):
        """按任务键移除单个任务（ctx.remove_job 用）"""
        self._jobs.pop(task_key, None)

    def pause_task(self, task_key: str):
        """暂停指定任务"""
        job = self._jobs.get(task_key)
        if job is not None:
            job['paused'] = True
            logger.debug(f"定时任务已暂停: {task_key}")

    def resume_task(self, task_key: str):
        """恢复指定任务"""
        job = self._jobs.get(task_key)
        if job is not None:
            job['paused'] = False
            job['last_fire'] = None
            logger.debug(f"定时任务已恢复: {task_key}")

    def get_jobs(self) -> list:
        """获取所有任务（兼容旧 APScheduler 形态：id / next_run / trigger）"""
        return [
            {'id': tid, 'next_run': None, 'trigger': ' '.join(info['cron'])}
            for tid, info in self._jobs.items()
        ]

    # ── 调度循环 ──

    async def _tick_loop(self):
        while self._running:
            try:
                await asyncio.sleep(5)
                if not self._running:
                    break
                now = datetime.now()
                minute_key = (now.year, now.month, now.day, now.hour, now.minute)
                for info in self._jobs.values():
                    if info['paused']:
                        continue
                    c = info['cron']
                    if (_field_match(now.minute, c[0], 0, 59) and
                            _field_match(now.hour, c[1], 0, 23) and
                            _field_match(now.day, c[2], 1, 31) and
                            _field_match(now.month, c[3], 1, 12) and
                            _field_match(now.weekday(), c[4], 0, 6)):
                        if info['last_fire'] != minute_key:
                            info['last_fire'] = minute_key
                            asyncio.ensure_future(
                                self._run_job(info['handler'], info['plugin_name'])
                            )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"调度器 tick 异常: {e}")
