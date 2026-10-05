"""
身份/群权限判断与群级插件开关（core/ctx 节点 8）

- is_group_admin / is_group_owner / is_superuser / is_blacklisted / get_user_role
- enable/disable/is_enabled/get_status 群级插件开关（委托给 plugin_loader）
"""


class RolesMixin:
    """ctx 上的身份判定与群级插件开关"""

    def is_group_admin(self, group_id: int, user_id: int) -> bool:
        """判断用户是否为群管理员或群主"""
        try:
            row = self._db.query_one(
                "SELECT role FROM group_members WHERE group_id=%s AND user_id=%s",
                (group_id, user_id)
            )
            return row and row['role'] in ('owner', 'admin')
        except Exception:
            return False

    def is_group_owner(self, group_id: int, user_id: int) -> bool:
        """判断用户是否为群主"""
        try:
            row = self._db.query_one(
                "SELECT role FROM group_members WHERE group_id=%s AND user_id=%s",
                (group_id, user_id)
            )
            return row and row['role'] == 'owner'
        except Exception:
            return False

    def is_superuser(self, user_id: int) -> bool:
        """判断用户是否为框架超管（从 users 表的 role 字段判断）"""
        try:
            row = self._db.query_one(
                "SELECT role FROM users WHERE user_id=%s", (user_id,)
            )
            return row and row.get('role') == 'super'
        except Exception:
            return False

    def is_blacklisted(self, user_id: int) -> bool:
        """判断用户是否在黑名单中"""
        try:
            row = self._db.query_one(
                "SELECT is_blacklist FROM users WHERE user_id=%s", (user_id,)
            )
            return row and row.get('is_blacklist') == 1
        except Exception:
            return False

    def get_user_role(self, group_id: int, user_id: int) -> str:
        """
        获取用户在群内的完整身份
        :return: "super">"owner">"admin">"member">"blacklist"
        """
        if self.is_superuser(user_id):
            return "super"
        if self.is_blacklisted(user_id):
            return "blacklist"
        if self.is_group_owner(group_id, user_id):
            return "owner"
        if self.is_group_admin(group_id, user_id):
            return "admin"
        return "member"

    # ---- 群级插件开关 ----

    def enable_plugin_in_group(self, plugin_name: str, group_id: int):
        """在指定群启用某个插件（仅管理员/群主可用）"""
        self._framework.plugin_loader.set_group_plugin_enabled(plugin_name, group_id, True)

    def disable_plugin_in_group(self, plugin_name: str, group_id: int):
        """在指定群禁用某个插件（仅管理员/群主可用）"""
        self._framework.plugin_loader.set_group_plugin_enabled(plugin_name, group_id, False)

    def is_plugin_enabled_in_group(self, plugin_name: str, group_id: int) -> bool:
        """检查插件在指定群是否启用"""
        return self._framework.plugin_loader.is_plugin_enabled_for_group(plugin_name, group_id)

    def get_plugin_status_list(self, group_id: int) -> dict:
        """获取指定群所有插件的启用状态"""
        settings = self._framework.plugin_loader.get_group_plugin_settings(group_id)
        disabled_plugins = {r['plugin_name'] for r in settings if not r['enabled']}
        result = {}
        for name in self._framework.plugin_loader.get_loaded_plugins():
            result[name] = name not in disabled_plugins
        return result
