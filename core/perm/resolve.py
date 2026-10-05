"""
权限解析主流程（core/perm 节点 4）

组合：用户直节点 → 默认组 + 内置角色组 → 继承展开 → 按 weight 降序 →
收集各来源节点（用户优先）→ 构造 PermissionSet。结果按 TTL 进进程缓存。
"""

import logging

from .models import (
    BUILTIN_GROUPS, ROLE_TO_GROUP, normalize_node,
    _now, _alive, _ctx_match, _bool_val, PermissionSet,
)
from .cache import (
    _perm_cache, _PERM_CACHE_TTL, _lazy_cleanup,
    invalidate_user, invalidate_groups, invalidate_all,
)
from .snapshot import _load_snapshot, _expand_inheritance

logger = logging.getLogger('zernus')

# 让 cache 的失效函数在 resolve 命名空间可用（保持旧调用方 import 路径稳定）
__all__ = ['resolve', 'invalidate_user', 'invalidate_groups', 'invalidate_all']


def resolve(db, user_id, context=None, role=None, use_cache=True) -> PermissionSet:
    """解析用户在指定上下文下的完整权限

    :param db: 数据库实例
    :param user_id: 用户 ID
    :param context: {'group': '123456', 'bot': 'main', 'msgtype': 'group'}，None 表示无上下文
    :param role: 框架身份（Event.role），用于注入内置角色组
    :return: PermissionSet
    """
    context = context or {}
    from .models import _ctx_signature
    sig = _ctx_signature(context)
    key = (user_id, sig, role or '')
    now = _now()

    if use_cache:
        hit = _perm_cache.get(key)
        if hit and now - hit[1] <= _PERM_CACHE_TTL:
            return hit[0]
        _lazy_cleanup(now)

    snap = _load_snapshot(db)
    groups_meta = dict(snap['groups'])
    groups_nodes = snap['nodes']

    # ── 1. 用户直接节点 + 组归属 ──
    direct = {}
    joined, excluded = [], set()
    try:
        rows = db.query("SELECT node, value, context_key, context_val, expire_at "
                        "FROM perm_user_nodes WHERE user_id = %s", (user_id,))
    except Exception as e:
        logger.debug(f"权限: 读取用户节点失败 {e}")
        rows = []

    for r in rows:
        if not _alive(r, now) or not _ctx_match(r, context):
            continue
        node = normalize_node(r.get('node'))
        if not node:
            continue
        if node.startswith('group.'):
            gname = node[6:]
            if _bool_val(r):
                joined.append(gname)
            else:
                excluded.add(gname)
        else:
            direct[node] = _bool_val(r)

    # ── 2. 默认组 + 内置角色组 ──
    for name, meta in groups_meta.items():
        if meta['is_default'] and name not in excluded:
            joined.append(name)

    builtin = ROLE_TO_GROUP.get(role or '')
    if builtin:
        joined.append(builtin)

    # ── 3. 沿 group.xxx 展开继承（BFS，环检测）──
    all_groups = _expand_inheritance(joined, excluded, groups_nodes, now, context)

    # ── 4. 按 weight 降序排序（同名按字典序，保证确定性）──
    def _weight_of(g):
        if g in BUILTIN_GROUPS:
            return BUILTIN_GROUPS[g]['weight']
        return (groups_meta.get(g) or {}).get('weight', 0)

    all_groups.sort(key=lambda g: (-_weight_of(g), g))

    # ── 5. 收集节点来源：用户直节点优先，其后各组按 weight 降序 ──
    sources = [direct] if direct else []
    merged = {}
    for g in all_groups:
        src = {}
        for r in groups_nodes.get(g, []):
            if not _alive(r, now) or not _ctx_match(r, context):
                continue
            node = normalize_node(r.get('node'))
            if not node or node.startswith('group.'):
                continue
            src[node] = _bool_val(r)
        # 内置组的身份节点
        bnode = BUILTIN_GROUPS.get(g, {}).get('node')
        if bnode:
            src[bnode] = True
        if src:
            sources.append(src)
        for k, v in src.items():
            merged.setdefault(k, v)

    # ── 6. 元信息（前缀/后缀取 primary group）──
    meta = {}
    for g in all_groups:
        if g.startswith('__'):
            continue
        m = groups_meta.get(g)
        if m:
            meta = {'prefix': m['prefix'], 'suffix': m['suffix']}
            break

    pset = PermissionSet(user_id, context, all_groups, merged, meta, sources)
    _perm_cache[key] = (pset, now)
    return pset
