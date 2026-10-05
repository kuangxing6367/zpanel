"""
权限变更审计（core/perm 节点 6）

审计写入失败不影响主流程，全部按 try/except 吞掉异常。
"""

import logging

from .models import _now

logger = logging.getLogger('zernus')


def audit(db, operator, action, target_type=None, target=None,
          node=None, value=None, context=None, detail=None):
    """写一条权限变更审计（失败不影响主流程）"""
    try:
        ctx_str = ''
        if context:
            ctx_str = ';'.join(f'{k}={v}' for k, v in context.items() if v)
        db.execute(
            "INSERT INTO perm_audit "
            "(operator, action, target_type, target, node, value, context, detail, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (str(operator or 'system'), action, target_type, str(target or ''),
             node, None if value is None else (1 if value else 0),
             ctx_str or None, detail, str(int(_now())))
        )
    except Exception as e:
        logger.warning(f"权限审计写入失败: {e}")


def list_audit(db, target_type=None, target=None, limit=100) -> list:
    try:
        if target_type and target:
            return db.query("SELECT * FROM perm_audit WHERE target_type=%s AND target=%s "
                            "ORDER BY id DESC LIMIT %s", (target_type, str(target), int(limit)))
        return db.query("SELECT * FROM perm_audit ORDER BY id DESC LIMIT %s", (int(limit),))
    except Exception as e:
        logger.warning(f"权限审计读取失败: {e}")
        return []
