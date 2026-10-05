"""
组管理（core/perm 节点 7）

组的创建/查询/更新/删除，以及组列表（带节点数）。
所有改动均触发缓存失效并写审计。
"""

import logging

from .models import _now
from .cache import invalidate_groups, invalidate_user
from .audit import audit

logger = logging.getLogger('zernus')


def list_groups(db) -> list:
    """全部权限组（含节点数），按 weight 降序"""
    try:
        rows = db.query("SELECT name, display_name, weight, prefix, suffix, is_default, created_at "
                        "FROM perm_groups")
    except Exception as e:
        logger.warning(f"权限: 读取组列表失败 {e}")
        return []
    counts = {}
    try:
        for r in db.query("SELECT group_name, COUNT(*) AS c FROM perm_group_nodes GROUP BY group_name"):
            counts[r['group_name']] = int(r['c'] or 0)
    except Exception:
        pass
    result = []
    for r in rows:
        d = dict(r)
        d['node_count'] = counts.get(r['name'], 0)
        d['builtin'] = False
        result.append(d)
    result.sort(key=lambda x: (-int(x.get('weight') or 0), x['name']))
    return result


def get_group(db, name) -> dict:
    try:
        return db.query_one("SELECT * FROM perm_groups WHERE name = %s", (name,))
    except Exception:
        return None


def create_group(db, name, display_name=None, weight=0, prefix=None, suffix=None,
                 is_default=0, operator='system'):
    name = str(name).strip()
    if not name or name.startswith('__'):
        raise ValueError("组名非法（不能为空或以 __ 开头，__ 前缀为内置组保留）")
    if get_group(db, name):
        raise ValueError(f"权限组 {name} 已存在")
    db.execute(
        "INSERT INTO perm_groups "
        "(name, display_name, weight, prefix, suffix, is_default, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (name, display_name or name, int(weight), prefix, suffix,
         1 if is_default else 0, str(int(_now())))
    )
    audit(db, operator, 'creategroup', 'group', name,
          detail=f"weight={weight} default={bool(is_default)}")
    invalidate_groups()
    invalidate_user()
    return True


def update_group(db, name, display_name=None, weight=None, prefix=None, suffix=None,
                 is_default=None, operator='system'):
    fields, args = [], []
    if display_name is not None:
        fields.append("display_name = %s")
        args.append(display_name)
    if weight is not None:
        fields.append("weight = %s")
        args.append(int(weight))
    if prefix is not None:
        fields.append("prefix = %s")
        args.append(prefix)
    if suffix is not None:
        fields.append("suffix = %s")
        args.append(suffix)
    if is_default is not None:
        fields.append("is_default = %s")
        args.append(1 if is_default else 0)
    if not fields:
        return False
    args.append(name)
    db.execute(f"UPDATE perm_groups SET {', '.join(fields)} WHERE name = %s", tuple(args))
    audit(db, operator, 'updategroup', 'group', name, detail=', '.join(
        f for f in fields))
    invalidate_groups()
    invalidate_user()
    return True


def delete_group(db, name, operator='system'):
    """删除组：同时清理该组节点、其他组对它的继承、以及用户身上对它的归属"""
    inherit_node = f"group.{name}"
    try:
        db.execute("DELETE FROM perm_group_nodes WHERE group_name = %s", (name,))
        db.execute("DELETE FROM perm_group_nodes WHERE node = %s", (inherit_node,))
        db.execute("DELETE FROM perm_user_nodes WHERE node = %s", (inherit_node,))
        db.execute("DELETE FROM perm_groups WHERE name = %s", (name,))
    except Exception as e:
        logger.warning(f"权限: 删除组 {name} 失败 {e}")
        raise
    audit(db, operator, 'deletegroup', 'group', name)
    invalidate_groups()
    invalidate_user()
    return True
