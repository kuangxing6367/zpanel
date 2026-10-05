# -*- coding: utf-8 -*-
"""任务调度（core/ctx 节点 17）

提供 ctx.add_job / ctx.remove_job，让插件以「一句调用」注册/注销
定时任务（无需手写 task 声明）。底层复用 framework.scheduler，
handler 可为模块级函数名（字符串）或直接传入可调用对象（闭包/局部函数）。
"""
import logging

logger = logging.getLogger('zernus')


class JobMixin:
    """ctx 上的定时任务快捷注册。"""

    def add_job(self, handler, cron_expression: str, description: str = '',
                job_id: str = None) -> str:
        """注册定时任务，返回任务键（用于 remove_job）。

        :param handler: 处理函数（可调用对象）或模块级函数名（字符串）
        :param cron_expression: 5 字段 cron 表达式（分 时 日 月 周）
        :param description: 说明
        :param job_id: 任务 id（缺省取 handler.__name__）；相同 id 重复注册会被覆盖
        """
        scheduler = getattr(self._framework, 'scheduler', None)
        if scheduler is None:
            raise RuntimeError("调度器未加载，无法 add_job")
        if isinstance(handler, str):
            handler_name = handler
            handler_obj = None
        else:
            handler_name = getattr(handler, '__name__', 'job')
            handler_obj = handler
        jid = job_id or handler_name
        task_info = {
            'id': jid,
            'plugin_name': self._plugin_name,
            'cron_expression': cron_expression,
            'handler': handler_obj,
            'handler_name': handler_name,
            'description': description or handler_name,
        }
        scheduler.add_plugin_task(task_info)
        return f"plugin_{self._plugin_name}_{jid}"

    def remove_job(self, job_id: str):
        """按 job_id 移除本插件的一个定时任务。"""
        scheduler = getattr(self._framework, 'scheduler', None)
        if scheduler is None:
            return
        scheduler.remove_job(f"plugin_{self._plugin_name}_{job_id}")
