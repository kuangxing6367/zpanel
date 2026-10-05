"""
插件生命周期 mixin：卸载与查询

负责卸载插件（清理调度器/DB/事件订阅/sys.modules/sys.path 并 gc），
以及查询已加载插件列表与模块引用。
"""
import gc
import logging
import os
import sys
from typing import Dict
from core.hooks import HookPoints

logger = logging.getLogger('zernus')


class LifecycleMixin:
    """插件卸载与查询。"""

    def unload_plugin(self, plugin_name: str):
        """卸载插件"""
        with self._lock:
            info = self._loaded_plugins.pop(plugin_name, None)
            if not info:
                return

        try:
            # 调用 on_unload（如果存在）
            module = info['module']
            if hasattr(module, 'on_unload') and callable(module.on_unload):
                module.on_unload()
        except Exception as e:
            logger.warning(f"[{plugin_name}] on_unload 异常: {e}")

        # 清理调度器中的定时任务
        try:
            self.framework.scheduler.remove_plugin_tasks(plugin_name)
        except Exception as e:
            logger.warning(f"[{plugin_name}] 清理调度器任务失败: {e}")

        # 清理数据库
        try:
            self.db.execute("DELETE FROM commands WHERE plugin_name = %s", (plugin_name,))
            self.db.execute("DELETE FROM tasks WHERE plugin_name = %s", (plugin_name,))
            # 注意：不删除 plugin_configs —— 卸载/重载/更新/禁用都应保留用户配置，
            # 只有真正删除插件（delete_plugin）时才清配置
            self.db.execute("UPDATE plugins SET status='stopped', has_register=0 WHERE plugin_name=%s", (plugin_name,))
        except Exception as e:
            logger.error(f"[{plugin_name}] 卸载清理失败: {e}")

        # 清理缺失依赖记录
        with self._lock:
            self._missing_deps.pop(plugin_name, None)
            self._conflict_deps.pop(plugin_name, None)
            self._isolated_plugins.discard(plugin_name)

        # 若该插件接管了前端，卸载后回退框架默认前端
        self.clear_override_webui(plugin_name)

        # 移除事件订阅
        self.framework.event_bus.unsubscribe_plugin(plugin_name)

        # 移除原始消息处理器
        try:
            self.framework.unregister_raw_message_handlers(plugin_name)
        except Exception as e:
            logger.warning(f"[{plugin_name}] 清理原始消息处理器失败: {e}")

        # 清理扩展点（hook）处理器，避免卸载后残留空引用
        try:
            self.framework.hooks.clear_plugin(plugin_name)
        except Exception as e:
            logger.warning(f"[{plugin_name}] 清理扩展点失败: {e}")

        # 清理 sys.modules：删除该插件目录下的所有模块（点分层级名/下划线别名/短名
        # 指向同一模块对象，按 __file__ 一次清净，避免热重载污染）
        self._purge_plugin_modules(
            plugin_name, info.get('path') or os.path.join(self.plugins_dir, plugin_name))

        # 清理 sys.path：移除该插件的目录（避免路径污染其他插件）
        try:
            plugin_path = info.get('path') or os.path.join(self.plugins_dir, plugin_name)
            norm = os.path.normpath(plugin_path)
            sys.path = [p for p in sys.path if os.path.normpath(p) != norm]
        except Exception:
            pass

        # 清理文件快照
        with self._lock:
            self._plugin_mtimes.pop(plugin_name, None)

        # 强制清理模块引用，触发垃圾回收
        # 防御插件未关闭的文件句柄 / socket 连接 / 长连接残留
        try:
            del module
        except NameError:
            pass
        # 连续两次 gc.collect()：第一次回收循环引用，第二次回收析构链
        collected = gc.collect()
        if collected > 0:
            logger.debug(f"[{plugin_name}] gc.collect() 回收了 {collected} 个对象")
        gc.collect()

        logger.info(f"[{plugin_name}] 已卸载")

        # 扩展点：插件卸载完成
        try:
            self.framework.hooks.trigger_sync(HookPoints.PLUGIN_UNLOAD, plugin_name)
        except Exception as e:
            logger.error(f"[{plugin_name}] 扩展点 [plugin.unload] 触发异常: {e}")

        # 插件已从内存移除，让路由表立即重建，避免路由到已卸载插件
        try:
            self.framework.router._invalidate_cache()
        except Exception:
            pass

    def get_loaded_plugins(self) -> Dict[str, dict]:
        """获取已加载插件列表"""
        with self._lock:
            return {
                name: {
                    'meta': info['meta'],
                    'priority': info['priority'],
                    'yaml': info.get('yaml', {}),
                }
                for name, info in self._loaded_plugins.items()
            }

    def get_plugin_module(self, plugin_name: str):
        """获取插件模块引用"""
        with self._lock:
            info = self._loaded_plugins.get(plugin_name)
            return info['module'] if info else None
