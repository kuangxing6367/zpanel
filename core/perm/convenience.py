"""
便捷查询 API（core/perm 节点 5）

对单次判定/组列表的常见封装，全部基于 resolve()。
"""

from .resolve import resolve


def has_perm(db, user_id, node, context=None, role=None) -> bool:
    """单点权限判断（未定义按拒绝）"""
    return resolve(db, user_id, context, role).has(node)


def check_perm(db, user_id, node, context=None, role=None):
    """三态权限判断：True / False / None"""
    return resolve(db, user_id, context, role).check(node)


def user_groups(db, user_id, context=None, role=None) -> list:
    """用户生效组（含继承展开，按 weight 降序）"""
    return list(resolve(db, user_id, context, role).groups)
