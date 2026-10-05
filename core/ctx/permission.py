"""
权限组查询（core/ctx 节点 4）

把 ctx 上的权限判断桥接到 core.perm 引擎（LuckPerms 风格）。
"""

import logging

from core.perm import has_perm, check_perm, user_groups

logger = logging.getLogger('zernus')


class PermissionMixin:
    """ctx.has_perm / check_perm / user_groups —— 委托给 core.perm 引擎"""

    def has_perm(self, user_id: int, node: str, context: dict = None, role: str = None) -> bool:
        """
        判断某用户是否拥有指定权限节点（未定义按拒绝处理）
        :param user_id: 用户 ID
        :param node: 权限节点，如 'myplugin.ban'
        :param context: 上下文 {'group': '123456', 'bot': 'main', 'msgtype': 'group'}
        :param role: 框架身份（super/owner/admin/member），用于注入内置角色组
        """
        return has_perm(self._db, user_id, node, context, role)

    def check_perm(self, user_id: int, node: str, context: dict = None, role: str = None):
        """三态权限查询：True=授予 / False=显式否决 / None=未定义"""
        return check_perm(self._db, user_id, node, context, role)

    def user_groups(self, user_id: int, context: dict = None, role: str = None) -> list:
        """用户的生效权限组（含继承展开，按 weight 降序）"""
        return user_groups(self._db, user_id, context, role)
