"""
权限缓存（core/perm 节点 2）

解析结果缓存 + 组快照缓存的失效与惰性清理逻辑。模块级字典，进程内共享。
"""

# ── 解析结果缓存 ──
# 缓存键：(user_id, 上下文本地键, role) -> (PermissionSet, ts)
_PERM_CACHE_TTL = 60.0
_PERM_CACHE_MAX = 5000
_perm_cache = {}
_perm_cache_checks = 0

# ── 组快照缓存 ──
# 全表读取，避免每个用户解析都重复拉组数据
_SNAPSHOT_TTL = 10.0
_snapshot_cache = {'ts': 0.0, 'groups': {}, 'nodes': {}}


def invalidate_user(user_id=None):
    """清除用户权限缓存（Web 端改动后调用，None=全部）"""
    if user_id is None:
        _perm_cache.clear()
    else:
        for k in [k for k in _perm_cache if k[0] == user_id]:
            _perm_cache.pop(k, None)


def invalidate_groups():
    """清除组快照缓存（改动组/组节点后调用）"""
    _snapshot_cache['ts'] = 0.0


def invalidate_all():
    invalidate_user()
    invalidate_groups()


def _lazy_cleanup(now: float):
    """惰性上限清理：每 256 次访问检查一次，防止长期运行内存无限增长"""
    global _perm_cache_checks
    _perm_cache_checks += 1
    if _perm_cache_checks % 256:
        return
    if len(_perm_cache) > _PERM_CACHE_MAX:
        for k in [k for k, v in _perm_cache.items() if now - v[1] > _PERM_CACHE_TTL]:
            _perm_cache.pop(k, None)
