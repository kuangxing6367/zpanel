"""
建表与迁移（core/storage 节点 3）

- _parse_sqlite_type：解析数据库配置（支持简写）
- init_db：初始化全局 Database 单例 + 自动建表与迁移
- _auto_create_tables / _migrate_*：框架扩展表与旧版兼容迁移

init_db 会写入 core.storage 包级 `db` 单例，供 framework/database 兼容壳层实时反射。
"""

import logging
import time

from .engine import Database

logger = logging.getLogger('zernus')


def _parse_sqlite_type(config: dict) -> dict:
    """
    解析 SQLite 数据库配置
    支持简写：database: path 或 database: {type: sqlite, path: xxx}
    """
    if isinstance(config, str):
        return {'type': 'sqlite', 'path': config}
    if isinstance(config, dict):
        cfg = dict(config)
        cfg.setdefault('type', 'sqlite')
        if cfg['type'] == 'sqlite':
            cfg.setdefault('path', 'data/zernus.db')
        return cfg
    return {'type': 'sqlite', 'path': 'data/zernus.db'}


def init_db(config: dict):
    """初始化数据库（全局单例）"""
    import core.storage as _storage

    # 解析配置
    db_config = _parse_sqlite_type(config)
    _storage.db = Database(db_config)

    # 自动检测并初始化数据库表（MySQL 5.5~8.0 / SQLite 全兼容）
    from core.storage.init_db import auto_init_database
    auto_init_database(_storage.db)

    # 创建框架扩展表 + 迁移（兼容旧版升级）
    _auto_create_tables(_storage.db)
    return _storage.db


def _auto_create_tables(database):
    """自动创建框架所需的扩展表"""
    # 列类型统一用 VARCHAR（SQLite 宽松类型同样兼容）
    tables = {
        'group_plugin_settings': """
            CREATE TABLE IF NOT EXISTS group_plugin_settings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id    INTEGER NOT NULL,
                plugin_name VARCHAR(64) NOT NULL,
                enabled     INTEGER DEFAULT 1,
                updated_at  VARCHAR(32),
                UNIQUE(group_id, plugin_name)
            )
        """,
        'ip_blacklist': """
            CREATE TABLE IF NOT EXISTS ip_blacklist (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ip          VARCHAR(64) NOT NULL,
                reason      VARCHAR(255),
                source      VARCHAR(32) DEFAULT 'manual',
                expires_at  VARCHAR(32),
                created_at  VARCHAR(32),
                updated_at  VARCHAR(32),
                UNIQUE(ip)
            )
        """,
        'perm_groups': """
            CREATE TABLE IF NOT EXISTS perm_groups (
                name         VARCHAR(64)  NOT NULL PRIMARY KEY,
                display_name VARCHAR(100) DEFAULT NULL,
                weight       INTEGER      DEFAULT 0,
                prefix       VARCHAR(64)  DEFAULT NULL,
                suffix       VARCHAR(64)  DEFAULT NULL,
                is_default   INTEGER      DEFAULT 0,
                created_at   VARCHAR(32)  DEFAULT NULL
            )
        """,
        'perm_group_nodes': """
            CREATE TABLE IF NOT EXISTS perm_group_nodes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                group_name  VARCHAR(64)  NOT NULL,
                node        VARCHAR(191) NOT NULL,
                value       INTEGER      DEFAULT 1,
                context_key VARCHAR(32)  DEFAULT NULL,
                context_val VARCHAR(64)  DEFAULT NULL,
                expire_at   VARCHAR(32)  DEFAULT NULL,
                created_at  VARCHAR(32)  DEFAULT NULL
            )
        """,
        'perm_user_nodes': """
            CREATE TABLE IF NOT EXISTS perm_user_nodes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     BIGINT       NOT NULL,
                node        VARCHAR(191) NOT NULL,
                value       INTEGER      DEFAULT 1,
                context_key VARCHAR(32)  DEFAULT NULL,
                context_val VARCHAR(64)  DEFAULT NULL,
                expire_at   VARCHAR(32)  DEFAULT NULL,
                created_at  VARCHAR(32)  DEFAULT NULL
            )
        """,
        'perm_tracks': """
            CREATE TABLE IF NOT EXISTS perm_tracks (
                name         VARCHAR(64)  NOT NULL PRIMARY KEY,
                display_name VARCHAR(100) DEFAULT NULL,
                groups_order VARCHAR(500) NOT NULL,
                created_at   VARCHAR(32)  DEFAULT NULL
            )
        """,
        'perm_audit': """
            CREATE TABLE IF NOT EXISTS perm_audit (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                operator    VARCHAR(100) DEFAULT NULL,
                action      VARCHAR(32)  DEFAULT NULL,
                target_type VARCHAR(16)  DEFAULT NULL,
                target      VARCHAR(100) DEFAULT NULL,
                node        VARCHAR(191) DEFAULT NULL,
                value       INTEGER      DEFAULT NULL,
                context     VARCHAR(120) DEFAULT NULL,
                detail      VARCHAR(500) DEFAULT NULL,
                created_at  VARCHAR(32)  DEFAULT NULL
            )
        """,
        'api_tokens': """
            CREATE TABLE IF NOT EXISTS api_tokens (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                token        VARCHAR(512) NOT NULL,
                name         VARCHAR(100) NOT NULL,
                role         VARCHAR(20)  DEFAULT 'admin',
                created_by   VARCHAR(100) DEFAULT NULL,
                created_at   VARCHAR(32)  DEFAULT NULL,
                expires_at   VARCHAR(32)  DEFAULT NULL,
                last_used_at VARCHAR(32)  DEFAULT NULL,
                is_active    INTEGER      DEFAULT 1,
                UNIQUE(token)
            )
        """,
        # 消息路由 / 插件系统核心表：保证即使 auto_init_database 因 admin_users+plugins 已存在而跳过，
        # 这些被 router / plugin_loader 自检直接查询的表也一定存在（杜绝「no such table」每分错报）。
        'plugins': """
            CREATE TABLE IF NOT EXISTS plugins (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                plugin_name  VARCHAR(50) NOT NULL,
                version      VARCHAR(20) DEFAULT NULL,
                author       VARCHAR(100) DEFAULT NULL,
                description  TEXT DEFAULT NULL,
                priority     INTEGER DEFAULT 50,
                status       VARCHAR(16) DEFAULT 'running',
                memory_usage DOUBLE DEFAULT 0,
                install_path VARCHAR(500) DEFAULT NULL,
                is_active    INTEGER DEFAULT 1,
                has_register INTEGER DEFAULT 0,
                loaded_at    VARCHAR(32) DEFAULT NULL,
                created_at   VARCHAR(32) DEFAULT NULL,
                updated_at   VARCHAR(32) DEFAULT NULL,
                UNIQUE(plugin_name)
            )
        """,
        'commands': """
            CREATE TABLE IF NOT EXISTS commands (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                plugin_name  VARCHAR(50) NOT NULL,
                pattern      VARCHAR(500) NOT NULL,
                alias        VARCHAR(500) DEFAULT NULL,
                description  VARCHAR(500) DEFAULT NULL,
                priority     INTEGER DEFAULT 50,
                handler      VARCHAR(100) NOT NULL,
                is_dynamic   INTEGER DEFAULT 0,
                require_level VARCHAR(20) DEFAULT '',
                require_perm VARCHAR(255) DEFAULT '',
                is_active    INTEGER DEFAULT 1,
                hit_count    INTEGER DEFAULT 0,
                created_at   VARCHAR(32) DEFAULT NULL
            )
        """,
        'tasks': """
            CREATE TABLE IF NOT EXISTS tasks (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                plugin_name     VARCHAR(50) NOT NULL,
                cron_expression VARCHAR(50) NOT NULL,
                handler         VARCHAR(100) NOT NULL,
                description     VARCHAR(500) DEFAULT NULL,
                is_active       INTEGER DEFAULT 1,
                last_run_at     VARCHAR(32) DEFAULT NULL,
                next_run_at     VARCHAR(32) DEFAULT NULL,
                run_count       INTEGER DEFAULT 0,
                last_status     VARCHAR(20) DEFAULT NULL,
                created_at      VARCHAR(32) DEFAULT NULL,
                updated_at      VARCHAR(32) DEFAULT NULL
            )
        """,
        'dynamic_commands': """
            CREATE TABLE IF NOT EXISTS dynamic_commands (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword      VARCHAR(200) NOT NULL,
                response     TEXT NOT NULL,
                match_type   VARCHAR(16) DEFAULT 'exact',
                handler      VARCHAR(100) DEFAULT '',
                plugin_name  VARCHAR(50) DEFAULT 'system',
                is_active    INTEGER DEFAULT 1,
                hit_count    INTEGER DEFAULT 0,
                created_at   VARCHAR(32) DEFAULT NULL,
                updated_at   VARCHAR(32) DEFAULT NULL
            )
        """,
    }

    # MySQL 模式下替换 AUTOINCREMENT → AUTO_INCREMENT
    if database.db_type == 'mysql':
        mysql_tables = {}
        for name, ddl in tables.items():
            ddl = ddl.replace('AUTOINCREMENT', 'AUTO_INCREMENT')
            mysql_tables[name] = ddl
        tables = mysql_tables
    for name, ddl in tables.items():
        try:
            database.execute(ddl)
            logger.debug(f"自动建表: {name}")
        except Exception as e:
            logger.warning(f"自动建表失败 [{name}]: {e}")

    _migrate_commands_table(database)
    _migrate_users_table(database)
    _migrate_commands_require_perm(database)
    _migrate_admin_users_table(database)
    _migrate_dynamic_commands_table(database)
    _migrate_dynamic_commands_handler(database)
    _migrate_perm_tables(database)
    _ensure_admin_account(database)


# 默认管理员（与 sql/init.sql 种子一致；密码 admin123，首次登录后请立刻修改）
_DEFAULT_ADMIN_USERNAME = 'admin'
_DEFAULT_ADMIN_HASH = (
    'pbkdf2_sha256$200000$94636c3c8d7aff1965bb27e9c7f5fb1c'
    '$d6b141cc4cb5c5c047a1e89fb10e9035060b4a2076d118f0dc81c8a667d29232'
)


def _ensure_admin_account(database):
    """确保存在可用的超级管理员账号（启动自愈，避免「建了库却登不进后台」）。

    三种情形：
    1. 无 admin_users 表 → 交给 init.sql 处理，跳过；
    2. 表存在但账号为空 → 用框架自带 pbkdf2 哈希种入默认账号；
    3. 存在遗留 bcrypt 账号且当前环境未安装 bcrypt（会彻底无法登录）：
       - 若该账号仍是「从未登录过的种子账号」，用默认密码就地重置为 pbkdf2；
       - 若账号已登录过（说明当时装过 bcrypt），只告警，绝不静默改密。
    """
    try:
        if not database.table_exists('admin_users'):
            return

        rows = database.query(
            "SELECT id, username, password_hash, token, last_login_at FROM admin_users"
        ) or []

        if not rows:
            database.execute(
                "INSERT INTO admin_users (username, password_hash, role) VALUES (%s, %s, %s)",
                (_DEFAULT_ADMIN_USERNAME, _DEFAULT_ADMIN_HASH, 'super'),
            )
            logger.warning(
                "[admin] 数据库无任何管理员账号，已种入默认账号 "
                f"{_DEFAULT_ADMIN_USERNAME}/admin123（请登录后立即修改密码）"
            )
            return

        from importlib.util import find_spec
        has_bcrypt = find_spec('bcrypt') is not None
        if has_bcrypt:
            return

        for row in rows:
            h = row['password_hash'] or ''
            if not h.startswith('$2'):
                continue
            never_logged_in = not row.get('token') and not row.get('last_login_at')
            if never_logged_in:
                database.execute(
                    "UPDATE admin_users SET password_hash = %s WHERE id = %s",
                    (_DEFAULT_ADMIN_HASH, row['id']),
                )
                logger.warning(
                    "[admin] 账号 %s 的密码为遗留 bcrypt 哈希，而当前环境未安装 bcrypt，"
                    "无法登录；该账号从未登录过，已按默认密码 admin123 重置为 pbkdf2。"
                    "请登录后立即修改密码。",
                    row['username'],
                )
            else:
                logger.error(
                    "[admin] 账号 %s 的密码为遗留 bcrypt 哈希，而当前环境未安装 bcrypt，"
                    "将无法登录。请执行 pip install bcrypt 恢复访问。",
                    row['username'],
                )
    except Exception as e:
        logger.warning(f"[admin] 管理员账号自愈检查跳过: {e}")


def _migrate_commands_table(database):
    """迁移 commands 表添加 require_level 列"""
    try:
        if database.table_exists('commands') and \
           not database.table_has_column('commands', 'require_level'):
            if database.db_type == 'sqlite':
                database.execute(
                    "ALTER TABLE commands ADD COLUMN require_level TEXT DEFAULT ''"
                )
            else:
                database.execute(
                    "ALTER TABLE commands ADD COLUMN require_level VARCHAR(20) DEFAULT '' "
                    "COMMENT '权限要求: admin=管理员/群主/超管, super=超管'"
                )
            logger.info("数据库迁移: commands 表添加 require_level 列")
    except Exception:
        pass


def _migrate_commands_require_perm(database):
    """迁移 commands 表添加 require_perm 列（权限节点要求，与 require_level 并存）"""
    try:
        if database.table_exists('commands') and \
           not database.table_has_column('commands', 'require_perm'):
            if database.db_type == 'sqlite':
                database.execute(
                    "ALTER TABLE commands ADD COLUMN require_perm TEXT DEFAULT ''"
                )
            else:
                database.execute(
                    "ALTER TABLE commands ADD COLUMN require_perm VARCHAR(255) DEFAULT '' "
                    "COMMENT '权限节点要求(LuckPerms风格), 空=不限制'"
                )
            logger.info("数据库迁移: commands 表添加 require_perm 列")
    except Exception:
        pass


def _migrate_users_table(database):
    """迁移 users 表添加 role 列"""
    try:
        if database.table_exists('users') and \
           not database.table_has_column('users', 'role'):
            if database.db_type == 'sqlite':
                database.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT ''")
            else:
                database.execute(
                    "ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT '' "
                    "COMMENT '权限角色: super=超级管理员, 空=普通用户'"
                )
            logger.info("数据库迁移: users 表添加 role 列")
    except Exception:
        pass


def _migrate_admin_users_table(database):
    """迁移 admin_users 表添加 token 和 token_created_at 列"""
    try:
        if not database.table_exists('admin_users'):
            return

        if not database.table_has_column('admin_users', 'token'):
            if database.db_type == 'sqlite':
                database.execute(
                    "ALTER TABLE admin_users ADD COLUMN token TEXT DEFAULT NULL"
                )
            else:
                database.execute(
                    "ALTER TABLE admin_users ADD COLUMN token VARCHAR(2048) DEFAULT NULL "
                    "COMMENT '登录令牌(2048位随机)'"
                )
            logger.info("数据库迁移: admin_users 表添加 token 列")

        if not database.table_has_column('admin_users', 'token_created_at'):
            if database.db_type == 'sqlite':
                database.execute(
                    "ALTER TABLE admin_users ADD COLUMN token_created_at TEXT DEFAULT NULL"
                )
            else:
                database.execute(
                    "ALTER TABLE admin_users ADD COLUMN token_created_at DATETIME DEFAULT NULL "
                    "COMMENT '令牌签发时间'"
                )
            logger.info("数据库迁移: admin_users 表添加 token_created_at 列")
    except Exception:
        pass


def _migrate_dynamic_commands_table(database):
    """迁移 dynamic_commands 表：match_type ENUM 增加 contains（更开放的匹配方式）"""
    try:
        if not database.table_exists('dynamic_commands'):
            return
        if database.db_type != 'mysql':
            return
        row = database.query_one(
            "SELECT COLUMN_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = 'dynamic_commands' AND COLUMN_NAME = 'match_type'"
        )
        col_type = (row or {}).get('COLUMN_TYPE', '') or ''
        if 'contains' in col_type:
            return
        database.execute(
            "ALTER TABLE dynamic_commands MODIFY COLUMN match_type "
            "ENUM('exact','prefix','contains','regex') DEFAULT 'exact' "
            "COMMENT '匹配方式'"
        )
        logger.info("数据库迁移: dynamic_commands.match_type ENUM 增加 contains")
    except Exception:
        pass


def _migrate_perm_tables(database):
    """权限系统（perm_*）建表后处理：补索引 + 播种默认组"""
    index_ddls = (
        "CREATE INDEX IF NOT EXISTS idx_pun_user ON perm_user_nodes (user_id)",
        "CREATE INDEX IF NOT EXISTS idx_pun_node ON perm_user_nodes (node)",
        "CREATE INDEX IF NOT EXISTS idx_pgn_group ON perm_group_nodes (group_name)",
        "CREATE INDEX IF NOT EXISTS idx_pgn_node ON perm_group_nodes (node)",
        "CREATE INDEX IF NOT EXISTS idx_pg_weight ON perm_groups (weight)",
        "CREATE INDEX IF NOT EXISTS idx_pa_target ON perm_audit (target_type, target)",
        "CREATE INDEX IF NOT EXISTS idx_pa_created ON perm_audit (created_at)",
    )
    for ddl in index_ddls:
        try:
            database.execute(ddl)
        except Exception:
            pass

    try:
        if not database.table_exists('perm_groups'):
            return
        row = database.query_one(
            "SELECT name FROM perm_groups WHERE name = %s", ('default',))
        if not row:
            database.execute(
                "INSERT INTO perm_groups "
                "(name, display_name, weight, prefix, suffix, is_default, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                ('default', '默认组', 0, None, None, 1, str(int(time.time())))
            )
            logger.info("数据库迁移: 播种默认权限组 default")
    except Exception:
        pass


def _migrate_dynamic_commands_handler(database):
    """迁移 dynamic_commands 表：增加 handler 列（关键词 handler 回调 plugin:func）"""
    try:
        if not database.table_exists('dynamic_commands'):
            return
        if database.table_has_column('dynamic_commands', 'handler'):
            return
        if database.db_type == 'sqlite':
            database.execute(
                "ALTER TABLE dynamic_commands ADD COLUMN handler TEXT DEFAULT ''")
        else:
            database.execute(
                "ALTER TABLE dynamic_commands ADD COLUMN handler VARCHAR(100) DEFAULT '' "
                "COMMENT '关键词handler回调 plugin:func'")
        logger.info("数据库迁移: dynamic_commands 表添加 handler 列")
    except Exception:
        pass
