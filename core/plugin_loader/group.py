"""
群级插件开关 mixin

提供「某插件在指定群是否启用」的查询与设置，并维护一份短期缓存（30 秒），
供消息路由热路径在查库前快速判定。
"""
import logging
import time

logger = logging.getLogger('zernus')


class GroupMixin:
    """群级插件开关（路由前检查）。"""

    def is_plugin_enabled_for_group(self, plugin_name: str, group_id: int) -> bool:
        """
        检查插件在指定群是否启用
        默认启用（表中无记录时视为启用）
        优先从缓存读取，30 秒刷新
        """
        if not group_id:
            return True  # 私聊不做限制

        # 刷新缓存
        self._refresh_group_plugin_cache()

        group_settings = self._group_plugin_cache.get(group_id)
        if group_settings is not None and plugin_name in group_settings:
            return group_settings[plugin_name]
        return True  # 无记录 = 启用

    def is_plugin_enabled_for_group_cached(self, plugin_name: str, group_id: int) -> bool:
        """
        检查插件在指定群是否启用（纯内存，不刷新缓存、不查库）
        供消息路由热路径使用：群开关缓存由路由表的后台刷新任务周期性维护
        默认启用（表中无记录时视为启用）
        """
        if not group_id:
            return True  # 私聊不做限制
        group_settings = self._group_plugin_cache.get(group_id)
        if group_settings is not None and plugin_name in group_settings:
            return group_settings[plugin_name]
        return True  # 无记录 = 启用

    def set_group_plugin_enabled(self, plugin_name: str, group_id: int, enabled: bool):
        """设置插件在指定群的启用/禁用状态，并立即更新缓存"""
        try:
            self.db.execute(
                "INSERT INTO group_plugin_settings (group_id, plugin_name, enabled) "
                "VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE enabled = %s",
                (group_id, plugin_name, 1 if enabled else 0, 1 if enabled else 0)
            )
        except Exception as e:
            logger.error(f"设置群级插件状态失败 [{plugin_name}][{group_id}]: {e}")
            raise

        # 立即更新缓存
        self._group_plugin_cache.setdefault(group_id, {})[plugin_name] = enabled

    def remove_group_plugin_setting(self, plugin_name: str, group_id: int):
        """删除群级插件开关记录（恢复默认=启用）"""
        try:
            self.db.execute(
                "DELETE FROM group_plugin_settings WHERE group_id = %s AND plugin_name = %s",
                (group_id, plugin_name)
            )
        except Exception as e:
            logger.error(f"删除群级插件设置失败 [{plugin_name}][{group_id}]: {e}")

        # 更新缓存
        group_settings = self._group_plugin_cache.get(group_id)
        if group_settings and plugin_name in group_settings:
            del group_settings[plugin_name]

    def get_group_plugin_settings(self, group_id: int = None) -> list:
        """获取群级插件设置列表"""
        if group_id:
            try:
                rows = self.db.query(
                    "SELECT plugin_name, enabled, updated_at "
                    "FROM group_plugin_settings WHERE group_id = %s "
                    "ORDER BY plugin_name",
                    (group_id,)
                )
                return rows
            except Exception as e:
                logger.error(f"查询群级插件设置失败 [{group_id}]: {e}")
                return []
        else:
            try:
                rows = self.db.query(
                    "SELECT group_id, plugin_name, enabled, updated_at "
                    "FROM group_plugin_settings ORDER BY group_id, plugin_name"
                )
                return rows
            except Exception as e:
                logger.error(f"查询群级插件设置失败: {e}")
                return []

    def _refresh_group_plugin_cache(self):
        """刷新群级插件开关缓存（最多 30 秒一次）"""
        now = time.time()
        if self._group_plugin_cache and (now - self._group_plugin_cache_time) < self._group_cache_ttl:
            return

        try:
            rows = self.db.query(
                "SELECT group_id, plugin_name, enabled FROM group_plugin_settings"
            )
            cache = {}
            for r in rows:
                gid = r['group_id']
                cache.setdefault(gid, {})[r['plugin_name']] = bool(r['enabled'])
            self._group_plugin_cache = cache
            self._group_plugin_cache_time = now
        except Exception as e:
            logger.warning(f"刷新群级插件缓存失败: {e}")
