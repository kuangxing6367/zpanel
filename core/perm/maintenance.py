"""
维护与事件桥接（core/perm 节点 10）

- cleanup_expired：清理过期节点（供定时调度调用）
- context_from_event：从 Event 对象构造权限上下文
"""

import logging

from .models import _now, _parse_ts
from .cache import invalidate_all

logger = logging.getLogger('zernus')


def cleanup_expired(db) -> int:
    """清理已过期的节点（供定时调度调用），返回清理条数"""
    now = _now()
    removed = 0
    try:
        for table in ('perm_user_nodes', 'perm_group_nodes'):
            rows = db.query(
                f"SELECT id, expire_at FROM {table} "
                f"WHERE expire_at IS NOT NULL AND expire_at != ''")
            for r in rows:
                exp = _parse_ts(r.get('expire_at'))
                if exp is not None and exp <= now:
                    db.execute(f"DELETE FROM {table} WHERE id = %s", (r['id'],))
                    removed += 1
        if removed:
            invalidate_all()
            logger.info(f"权限: 清理过期节点 {removed} 条")
    except Exception as e:
        logger.warning(f"权限: 清理过期节点失败 {e}")
    return removed


def context_from_event(ev) -> dict:
    """从 Event 对象构造上下文（供 Event.has_perm 使用）"""
    ctx = {}
    if getattr(ev, 'bot_name', None):
        ctx['bot'] = str(ev.bot_name)
    if getattr(ev, 'group_id', 0):
        ctx['group'] = str(ev.group_id)
    if getattr(ev, 'message_type', ''):
        ctx['msgtype'] = str(ev.message_type)
    return ctx
