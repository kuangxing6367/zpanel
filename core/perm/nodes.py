"""
节点管理（core/perm 节点 8）

组节点 / 用户节点的 set/unset（upsert 幂等：先删同键旧记录再插入），
以及用户节点列表、用户-组归属的增删（组归属即 `group.<name>` 用户节点）。
"""

import logging

from .models import normalize_node, _now
from .cache import invalidate_groups, invalidate_user
from .audit import audit

logger = logging.getLogger('zernus')


def _delete_node(db, table, key_field, key_val, node, ctx_key, ctx_val):
    """删除同（目标, 节点, 上下文）的旧记录，保证 upsert 幂等"""
    if ctx_key:
        db.execute(
            f"DELETE FROM {table} WHERE {key_field} = %s AND node = %s "
            f"AND context_key = %s AND context_val = %s",
            (key_val, node, ctx_key, ctx_val))
    else:
        db.execute(
            f"DELETE FROM {table} WHERE {key_field} = %s AND node = %s "
            f"AND (context_key IS NULL OR context_key = '')",
            (key_val, node))


def set_group_node(db, group_name, node, value=True, ctx_key=None, ctx_val=None,
                   expire_at=None, operator='system'):
    node = normalize_node(node)
    if not node:
        raise ValueError("节点名不能为空")
    ctx_key = (ctx_key or '').strip() or None
    ctx_val = None if ctx_key is None else str(ctx_val or '')
    _delete_node(db, 'perm_group_nodes', 'group_name', group_name, node, ctx_key, ctx_val)
    db.execute(
        "INSERT INTO perm_group_nodes "
        "(group_name, node, value, context_key, context_val, expire_at, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (group_name, node, 1 if value else 0, ctx_key, ctx_val,
         None if expire_at is None else str(int(expire_at)), str(int(_now())))
    )
    audit(db, operator, 'set', 'group', group_name, node, value,
          {ctx_key: ctx_val} if ctx_key else None)
    invalidate_groups()
    invalidate_user()
    return True


def unset_group_node(db, group_name, node, ctx_key=None, ctx_val=None, operator='system'):
    node = normalize_node(node)
    ctx_key = (ctx_key or '').strip() or None
    ctx_val = None if ctx_key is None else str(ctx_val or '')
    _delete_node(db, 'perm_group_nodes', 'group_name', group_name, node, ctx_key, ctx_val)
    audit(db, operator, 'unset', 'group', group_name, node, None,
          {ctx_key: ctx_val} if ctx_key else None)
    invalidate_groups()
    invalidate_user()
    return True


def set_user_node(db, user_id, node, value=True, ctx_key=None, ctx_val=None,
                  expire_at=None, operator='system'):
    node = normalize_node(node)
    if not node:
        raise ValueError("节点名不能为空")
    ctx_key = (ctx_key or '').strip() or None
    ctx_val = None if ctx_key is None else str(ctx_val or '')
    _delete_node(db, 'perm_user_nodes', 'user_id', user_id, node, ctx_key, ctx_val)
    db.execute(
        "INSERT INTO perm_user_nodes "
        "(user_id, node, value, context_key, context_val, expire_at, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (int(user_id), node, 1 if value else 0, ctx_key, ctx_val,
         None if expire_at is None else str(int(expire_at)), str(int(_now())))
    )
    audit(db, operator, 'set', 'user', user_id, node, value,
          {ctx_key: ctx_val} if ctx_key else None)
    invalidate_user(user_id)
    return True


def unset_user_node(db, user_id, node, ctx_key=None, ctx_val=None, operator='system'):
    node = normalize_node(node)
    ctx_key = (ctx_key or '').strip() or None
    ctx_val = None if ctx_key is None else str(ctx_val or '')
    _delete_node(db, 'perm_user_nodes', 'user_id', user_id, node, ctx_key, ctx_val)
    audit(db, operator, 'unset', 'user', user_id, node, None,
          {ctx_key: ctx_val} if ctx_key else None)
    invalidate_user(user_id)
    return True


def list_user_nodes(db, user_id) -> list:
    try:
        return db.query("SELECT * FROM perm_user_nodes WHERE user_id = %s ORDER BY id",
                        (int(user_id),))
    except Exception:
        return []


def add_user_group(db, user_id, group_name, ctx_key=None, ctx_val=None,
                   expire_at=None, operator='system'):
    return set_user_node(db, user_id, f"group.{group_name}", True, ctx_key, ctx_val,
                         expire_at, operator)


def remove_user_group(db, user_id, group_name, ctx_key=None, ctx_val=None, operator='system'):
    unset_user_node(db, user_id, f"group.{group_name}", ctx_key, ctx_val, operator)
    audit(db, operator, 'removegroup', 'user', user_id, f"group.{group_name}")
    invalidate_user(user_id)
    return True
