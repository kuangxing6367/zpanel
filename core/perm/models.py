"""
权限模型与底层工具（core/perm 节点 1）

对外暴露：
- 常量：BUILTIN_GROUPS / ROLE_TO_GROUP / CONTEXT_KEYS
- 工具：normalize_node / _now / _parse_ts / _alive / _ctx_match / _ctx_signature / _bool_val
- 结果类型：PermissionSet / _resolve_in_source

本模块不依赖任何其它 perm 子模块，是整棵依赖树的根。
"""

import time

logger = __import__('logging').getLogger('zernus')

# ── 内置角色组 ──────────────────────────────────────────────
# inherits 顺着链表即得到全部祖先节点，因此 super 自动拥有 owner/admin/member 的一切
BUILTIN_GROUPS = {
    '__member': {'weight': 0,   'display_name': '成员',   'inherits': [],           'node': 'zernus.role.member'},
    '__admin':  {'weight': 20,  'display_name': '群管理', 'inherits': ['__member'], 'node': 'zernus.role.admin'},
    '__owner':  {'weight': 30,  'display_name': '群主',   'inherits': ['__admin'],  'node': 'zernus.role.owner'},
    '__super':  {'weight': 100, 'display_name': '超级管理员', 'inherits': ['__owner'], 'node': 'zernus.role.super'},
}

# Event.role -> 内置组名。blacklist 由 router 在权限判定之前拦截，
# 这里退化为 member，保证语义与「黑名单不参与权限计算」一致
ROLE_TO_GROUP = {
    'member': '__member',
    'admin': '__admin',
    'owner': '__owner',
    'super': '__super',
    'blacklist': '__member',
}

CONTEXT_KEYS = ('group', 'bot', 'msgtype')


# ═══════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════

def normalize_node(node) -> str:
    """节点规范化：去空白 + 转小写（与 LuckPerms 一致，权限节点大小写不敏感）"""
    return (str(node or '')).strip().lower()


def _now() -> float:
    return time.time()


def _parse_ts(val):
    """解析 expire_at（VARCHAR 存的 unix 时间戳字符串），失败/空返回 None"""
    if val is None or val == '':
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _alive(row, now: float) -> bool:
    """行是否未过期"""
    exp = _parse_ts(row.get('expire_at'))
    return exp is None or exp > now


def _ctx_match(row, context: dict) -> bool:
    """行的上下文是否与当前环境匹配（上下文为空=全局，永远匹配）"""
    key = row.get('context_key')
    val = row.get('context_val')
    if not key or val is None or val == '':
        return True
    if not context:
        return False
    return str(context.get(key, '')).lower() == str(val).lower()


def _ctx_signature(context: dict) -> str:
    """上下文指纹，用于缓存键（只取受支持的维度，顺序固定）"""
    if not context:
        return ''
    return '|'.join(f'{k}={context.get(k, "")}' for k in CONTEXT_KEYS if context.get(k))


def _bool_val(row) -> bool:
    v = row.get('value')
    return False if v in (0, '0', False) else True


# ═══════════════════════════════════════════════════════════
# 解析结果
# ═══════════════════════════════════════════════════════════

class PermissionSet:
    """一次权限解析的结果（不可变快照）

    :ivar user_id: 用户 ID
    :ivar context: 解析时使用的上下文字典
    :ivar groups:  生效组名列表，按 weight 降序
    :ivar nodes:   合并后的节点表 {node: True/False}
    """

    # _sources：按优先级排列的多个节点表（用户直节点 → 各组按 weight 降序）
    # 必须在类定义时就声明，事后追加 __slots__ 不会生成描述符
    __slots__ = ('user_id', 'context', 'groups', 'nodes', '_meta', '_sources')

    def __init__(self, user_id, context, groups, nodes, meta, sources=None):
        self.user_id = user_id
        self.context = context or {}
        self.groups = groups
        self.nodes = nodes
        self._meta = meta or {}
        self._sources = sources or []

    # ---- 查询 ----

    def check(self, node: str):
        """三态查询：True=授予 / False=显式否决 / None=未定义"""
        n = normalize_node(node)
        if not n:
            return None
        # 节点表已按来源顺序合并：先写入的优先级高，命中即返回
        for src in self._sources:
            hit = _resolve_in_source(src, n)
            if hit is not None:
                return hit
        return None

    def has(self, node: str) -> bool:
        """二态查询：未定义按拒绝处理（与 LuckPerms 默认行为一致）"""
        return self.check(node) is True

    def has_any(self, *nodes) -> bool:
        return any(self.has(n) for n in nodes)

    def has_all(self, *nodes) -> bool:
        return all(self.has(n) for n in nodes)

    # ---- 组信息 ----

    @property
    def primary_group(self) -> str:
        """权重最高且非内置的组；没有则回退 default"""
        for g in self.groups:
            if not g.startswith('__'):
                return g
        return 'default'

    def in_group(self, name: str) -> bool:
        """是否在指定组内（groups 已包含继承展开的结果，无需二次展开）"""
        return normalize_node(name) in self.groups

    @property
    def prefix(self) -> str:
        return self._meta.get('prefix') or ''

    @property
    def suffix(self) -> str:
        return self._meta.get('suffix') or ''

    def to_dict(self) -> dict:
        return {
            'user_id': self.user_id,
            'context': self.context,
            'groups': list(self.groups),
            'primary_group': self.primary_group,
            'nodes': {k: v for k, v in self.nodes.items()},
        }

    def __repr__(self):
        return f'<PermissionSet user={self.user_id} groups={self.groups} nodes={len(self.nodes)}>'


def _resolve_in_source(nodes, node: str):
    """在单一来源内按精确度解析节点

    精确度：精确(0) > 段级通配(1..n，前缀越长越精确) > 全局 `*`(999)
    同一精确度下 value=0（否决）优先
    """
    cands = []
    if node in nodes:
        cands.append((0, nodes[node]))
    parts = node.split('.')
    for i in range(len(parts) - 1, 0, -1):
        wildcard = '.'.join(parts[:i]) + '.*'
        if wildcard in nodes:
            cands.append((len(parts) - i, nodes[wildcard]))
    if '*' in nodes:
        cands.append((999, nodes['*']))
    if not cands:
        return None
    best = min(s for s, _ in cands)
    vals = [v for s, v in cands if s == best]
    return False if False in vals else True
