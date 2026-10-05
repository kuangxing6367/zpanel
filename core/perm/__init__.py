"""
权限系统（LuckPerms 风格）— core/perm 公共入口

本包是权限引擎的唯一实现位置，按单一职责拆成若干细模块：
- models      常量 + 工具 + PermissionSet 结果类型（依赖树根）
- cache       解析缓存 / 组快照缓存的失效与惰性清理
- snapshot    组快照加载 + 继承展开
- resolve     解析主流程
- convenience has_perm / check_perm / user_groups 便捷封装
- audit       审计写入与读取
- groups      组管理（增删改查）
- nodes       组/用户节点管理（upsert 幂等）
- tracks      升降级轨道
- maintenance 过期清理 + 事件上下文桥接

framework/perm.py 仅做 `from core.perm import *` 兼容重导出，保证既有调用方零改动。
"""

from .models import (
    BUILTIN_GROUPS, ROLE_TO_GROUP, CONTEXT_KEYS,
    normalize_node, _now, _parse_ts, _alive, _ctx_match, _ctx_signature, _bool_val,
    PermissionSet, _resolve_in_source,
)
from .cache import (
    _PERM_CACHE_TTL, _PERM_CACHE_MAX, _perm_cache, _perm_cache_checks,
    _SNAPSHOT_TTL, _snapshot_cache,
    invalidate_user, invalidate_groups, invalidate_all, _lazy_cleanup,
)
from .snapshot import _load_snapshot, _builtin_group_meta, _expand_inheritance
from .resolve import resolve
from .convenience import has_perm, check_perm, user_groups
from .audit import audit, list_audit
from .groups import list_groups, get_group, create_group, update_group, delete_group
from .nodes import (
    _delete_node, set_group_node, unset_group_node, set_user_node,
    unset_user_node, list_user_nodes, add_user_group, remove_user_group,
)
from .tracks import (
    list_tracks, get_track, save_track, delete_track, _track_step,
    promote, demote,
)
from .maintenance import cleanup_expired, context_from_event

__all__ = [
    'BUILTIN_GROUPS', 'ROLE_TO_GROUP', 'CONTEXT_KEYS',
    'normalize_node', '_now', '_parse_ts', '_alive', '_ctx_match', '_ctx_signature', '_bool_val',
    'PermissionSet', '_resolve_in_source',
    '_PERM_CACHE_TTL', '_PERM_CACHE_MAX', '_perm_cache', '_perm_cache_checks',
    '_SNAPSHOT_TTL', '_snapshot_cache',
    'invalidate_user', 'invalidate_groups', 'invalidate_all', '_lazy_cleanup',
    '_load_snapshot', '_builtin_group_meta', '_expand_inheritance',
    'resolve',
    'has_perm', 'check_perm', 'user_groups',
    'audit', 'list_audit',
    'list_groups', 'get_group', 'create_group', 'update_group', 'delete_group',
    '_delete_node', 'set_group_node', 'unset_group_node', 'set_user_node',
    'unset_user_node', 'list_user_nodes', 'add_user_group', 'remove_user_group',
    'list_tracks', 'get_track', 'save_track', 'delete_track', '_track_step',
    'promote', 'demote',
    'cleanup_expired', 'context_from_event',
]
