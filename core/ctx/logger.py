"""
插件日志适配器 + 异步执行线程池（core/ctx 节点 1）

- PluginLogger：自动给每条日志加插件名前缀，暴露 .info/.warning/.error/.debug/.exception
- _async_executor：全局线程池，供 run_async 提交耗时任务，不阻塞主消息处理流程
"""

import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger('zernus')

# 全局线程池，用于异步执行耗时操作（如图片渲染），不阻塞主消息处理流程
_async_executor = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix='async_plugin'
)


class PluginLogger:
    """插件日志适配器：提供 .info/.warning/.error/.debug 接口，自动加插件名前缀"""

    def __init__(self, plugin_name: str):
        self._name = plugin_name

    def info(self, msg, *args, **kwargs):
        logger.info(f"[{self._name}] {msg}", *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        logger.warning(f"[{self._name}] {msg}", *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        logger.error(f"[{self._name}] {msg}", *args, **kwargs)

    def debug(self, msg, *args, **kwargs):
        logger.debug(f"[{self._name}] {msg}", *args, **kwargs)

    def warn(self, msg, *args, **kwargs):
        logger.warning(f"[{self._name}] {msg}", *args, **kwargs)

    def exception(self, msg, *args, **kwargs):
        logger.exception(f"[{self._name}] {msg}", *args, **kwargs)
