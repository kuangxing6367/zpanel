"""
插件操作审计（core/ctx 节点 10）

audit_log 写入 audit_logs 表，失败不影响主流程。
"""

import json
import logging

logger = logging.getLogger('zernus')


class AuditMixin:
    """ctx.audit_log —— 记录插件自身操作审计"""

    def audit_log(self, action: str, target_type: str = None,
                  target_name: str = None, detail: dict = None,
                  result: str = 'success', error_message: str = None):
        """
        记录插件操作审计日志
        无需管理员上下文，插件可以记录自己的操作（如数据修改、配置变更等）
        """
        try:
            self._db.execute(
                "INSERT INTO audit_logs (admin_id, admin_name, action, target_type, target_name, "
                "detail, result, error_message) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (0, f"plugin:{self._plugin_name}", f"plugin.{action}",
                 target_type, target_name,
                 json.dumps(detail, ensure_ascii=False) if detail else None,
                 result, error_message)
            )
        except Exception as e:
            logger.warning(f"[{self._plugin_name}] 审计日志写入失败: {e}")
