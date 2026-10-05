"""
存储层（core/storage）公共入口

原 framework/database/db.py 已按职责拆为：
- dialect     纯 SQL 方言翻译（MySQL↔SQLite，无状态）
- engine      Database 连接引擎（双后端/连接池/事务/自动重连）
- migrations  建表与旧版兼容迁移、全局单例初始化

本包对外暴露与旧 framework.database.db 完全一致的命名空间，
framework/database/db.py 仅做兼容重导出（并用模块级 __getattr__ 让 db 单例实时反射）。
"""

from .dialect import (
    _translate_sql_for_sqlite, _translate_sql_for_mysql, _replace_now,
    _strip_mysql_ddl_syntax, _convert_placeholders, _mysql_prefix_indexes,
    _is_ddl_or_dml, _split_top_level, _if_to_case, _translate_mysql_funcs,
    _on_duplicate_to_sqlite,
)
from .engine import Database
from .rate_limit import RateLimiter, RateLimitTimeout
from .migrations import (
    init_db, _auto_create_tables, _parse_sqlite_type,
    _migrate_commands_table, _migrate_commands_require_perm, _migrate_users_table,
    _migrate_admin_users_table, _migrate_dynamic_commands_table,
    _migrate_dynamic_commands_handler, _migrate_perm_tables,
)

# 全局 Database 单例（由 init_db 写入）
db = None

__all__ = [
    'Database',
    'RateLimiter',
    'RateLimitTimeout',
    'init_db',
    '_auto_create_tables',
    '_parse_sqlite_type',
    '_translate_sql_for_sqlite',
    '_translate_sql_for_mysql',
    '_replace_now',
    '_strip_mysql_ddl_syntax',
    '_convert_placeholders',
    '_mysql_prefix_indexes',
    '_is_ddl_or_dml',
    '_split_top_level',
    '_if_to_case',
    '_translate_mysql_funcs',
    '_on_duplicate_to_sqlite',
    'db',
]
