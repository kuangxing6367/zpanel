"""
组快照与继承展开（core/perm 节点 3）

把「全部组定义 + 组节点」整表读入进程内缓存（10s TTL，避免按组逐条查库），
并提供沿 group.xxx 的 BFS 继承展开（带环检测）。
"""

import logging

from .models import BUILTIN_GROUPS, _now, _alive, _ctx_match, _bool_val
from .cache import _snapshot_cache, _SNAPSHOT_TTL

logger = logging.getLogger('zernus')


def _load_snapshot(db) -> dict:
    """加载全部组定义与组节点（表小，整体缓存 10s，避免按组逐条查库）"""
    now = _now()
    if _snapshot_cache['ts'] and now - _snapshot_cache['ts'] <= _SNAPSHOT_TTL:
        return _snapshot_cache

    groups = {}
    try:
        for r in db.query("SELECT name, display_name, weight, prefix, suffix, is_default "
                          "FROM perm_groups"):
            groups[r['name']] = {
                'name': r['name'],
                'display_name': r.get('display_name') or r['name'],
                'weight': int(r.get('weight') or 0),
                'prefix': r.get('prefix') or '',
                'suffix': r.get('suffix') or '',
                'is_default': 1 if r.get('is_default') else 0,
                'builtin': False,
            }
    except Exception as e:
        logger.debug(f"权限: 读取 perm_groups 失败 {e}")

    nodes = {}
    try:
        for r in db.query("SELECT group_name, node, value, context_key, context_val, expire_at "
                          "FROM perm_group_nodes"):
            nodes.setdefault(r['group_name'], []).append(r)
    except Exception as e:
        logger.debug(f"权限: 读取 perm_group_nodes 失败 {e}")

    _snapshot_cache['ts'] = now
    _snapshot_cache['groups'] = groups
    _snapshot_cache['nodes'] = nodes
    return _snapshot_cache


def _builtin_group_meta(name: str) -> dict:
    b = BUILTIN_GROUPS[name]
    return {
        'name': name,
        'display_name': b['display_name'],
        'weight': b['weight'],
        'prefix': '',
        'suffix': '',
        'is_default': 0,
        'builtin': True,
    }


def _expand_inheritance(joined, excluded, groups_nodes, now, context):
    """BFS 展开 group.xxx 继承链，带环检测；被显式否决的组及其子节点不生效"""
    result, seen, queue = [], set(), list(joined)
    while queue:
        g = queue.pop(0)
        if g in seen or g in excluded:
            continue
        seen.add(g)
        result.append(g)
        for r in groups_nodes.get(g, []):
            if not _alive(r, now) or not _ctx_match(r, context):
                continue
            node = (str(r.get('node') or '')).strip().lower()
            if not node or not node.startswith('group.') or not _bool_val(r):
                continue
            parent = node[6:]
            if parent not in seen:
                queue.append(parent)
    # 内置组的静态继承（__admin ⊃ __member 等）
    added = True
    while added:
        added = False
        for g in list(result):
            for parent in BUILTIN_GROUPS.get(g, {}).get('inherits', []):
                if parent not in result:
                    result.append(parent)
                    added = True
    return result
