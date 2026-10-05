# -*- coding: utf-8 -*-
"""
compat — 版本兼容层（机制包，零框架依赖，单文件）

问题
----
同一种软件的不同版本，配置位置、服务名、校验命令、语言/SQL 语法、默认行为
都不一样。若把这些差异写成 `if version >= 8:` 散落在各处，加一个新版本就得
满仓库找分支 —— 而且「8.4 到底和 8.0 差在哪」这个问题没人能一眼回答。

做法
----
**一个版本 → 一个兼容层。** 每个层是 `LAYERS` 里的一条声明，写明
「管哪个版本区间」和「这个版本区间的事实」。上层只问 `resolve(family, version)`，
拿到的永远是一个层（兜底层保证不 miss）；新增版本 = 加一条声明，操作代码一行不动。

与 svcmgr 的关系
----------------
`svcmgr` 是「按平台分层」，这里是「按版本分层」，同一个门面/委派套路。
差别在于版本层数量多、且几乎是纯数据，所以不拆成几十个独立包，
而是本包内的一张声明表 —— 包外接口不变。

关于内容来源
------------
层里写的是**软件自身的客观事实**（上游发行说明、发行版默认约定），
例如「MySQL 8.0 默认认证插件是 caching_sha2_password」。
它不是任何商业面板的布局或配置格式 —— 本项目不采用他人面板的目录结构，
只清点软件自己安装在哪。
"""
from __future__ import annotations

import re

__version__ = "1.0.0"

# ══════════════════════════════════════════════════════════
# 版本规约
# ══════════════════════════════════════════════════════════
# 支持写法：
#   "8.4"        前缀匹配（8.4 / 8.4.1 / 8.4.33 都命中，8.40 不命中）
#   "7.x" "7.*"  同上，显式通配
#   ">=8.0 <8.2" 区间（空格或逗号 = 且）
#   "8.4+"       >=8.4 的简写
#   ""  "*"      通配（兜底层用）
_NUM = re.compile(r"^[vV]?\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def _parts(s) -> tuple:
    """拆出前导数字段，返回 (tuple, 段数)。'v8.2.11-fpm' → ((8,2,11), 3)。"""
    m = _NUM.match(str(s or "").strip())
    if not m:
        return (), 0
    segs = [int(g) for g in m.groups() if g is not None]
    return tuple(segs), len(segs)


def parse(v) -> tuple:
    """版本 → 三段整数元组（不足补 0）。无法解析时给 (0,0,0)。"""
    p, n = _parts(v)
    if not n:
        return (0, 0, 0)
    return (p + (0, 0, 0))[:3]


def compare(a, b) -> int:
    """版本比较：a<b → -1，a==b → 0，a>b → 1。"""
    x, y = parse(a), parse(b)
    return (x > y) - (x < y)


def _clause(vp: tuple, op: str, operand: str) -> bool:
    """单条子句判定。vp 是已解析的版本段元组。"""
    operand = str(operand or "").strip()
    if not operand:
        return True
    # 通配：7.x / 7.*  → 取 x 前的数字段做前缀匹配
    low = operand.lower()
    if "x" in low or "*" in low:
        keep = []
        for seg in low.replace("*", "x").split("."):
            if seg == "x" or not seg.isdigit():
                break
            keep.append(int(seg))
        if not keep:
            return True
        return tuple(vp[: len(keep)]) == tuple(keep)
    # 裸写法 / =  / ~=  / ^ ：一律当前缀匹配（用户说 "7.4" 意思是 7.4 这一支）
    if op in ("", "=", "==", "~=", "^"):
        op_parts, on = _parts(operand)
        if not on:
            return True
        return tuple(vp[:on]) == op_parts
    a = (vp + (0, 0, 0))[:3]
    b = parse(operand)
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    return False


def satisfies(version, spec) -> bool:
    """版本是否落在规约内。

    规约语法：``|`` = 或，空格 / 逗号 = 且，``*`` 或空串 = 通配。
    例：``">=8.0 <8.2"``、``"1.8 | 8"``（Java 8 的版本串既可能是 1.8.x 也可能是 8.x）、
    ``"7.4"``、``"8.4+"``。
    """
    spec = str(spec or "").strip()
    if not spec or spec == "*":
        return True
    vp, n = _parts(version)
    if not n:
        return False
    for group in spec.split("|"):
        group = group.strip()
        if not group:
            continue
        if _group_ok(vp, group):
            return True
    return False


def _group_ok(vp: tuple, group: str) -> bool:
    """一个「或」分组内的所有子句都必须成立（且）。"""
    for tok in re.split(r"[\s,]+", group):
        if not tok:
            continue
        if tok.endswith("+"):                    # "8.4+" → ">=8.4"
            op, operand = ">=", tok[:-1]
        else:
            m = re.match(r"^(>=|<=|>|<|==|=|~=|\^)?\s*(.+)$", tok)
            if not m:
                continue
            op, operand = (m.group(1) or ""), m.group(2)
        if not _clause(vp, op, operand):
            return False
    return True


def compact(version) -> str:
    """紧凑版本号：8.4 → '84'（用于服务名/套接字名里的版本后缀）。"""
    p, n = _parts(version)
    if not n:
        return ""
    maj = p[0]
    mnr = p[1] if n > 1 else 0
    return f"{maj}{mnr:02d}" if mnr >= 10 else f"{maj}{mnr}"


# ══════════════════════════════════════════════════════════
# 家族：跨版本共享的事实（可执行名、候选安装根、配置位置约定）
# ══════════════════════════════════════════════════════════
# 路径里的占位符：{ver}=8.4  {compact}=84  {root}=实际安装根
FAMILY = {
    "php": {
        "label": "PHP",
        "exes": ["php", "php-cgi", "php8", "php7", "php-fpm"],
        "roots_nix": ["/usr/local/php", "/usr/local/php*", "/opt/php*",
                      "/usr/local/Cellar/php", "/usr/local/Cellar/php@*",
                      "/usr/local/opt/php@*"],
        "roots_win": [r"C:\php", r"C:\php*", r"C:\Program Files\php", r"C:\phpstudy*"],
        "conf_nix": ["/etc/php/{ver}/fpm/php.ini", "/etc/php/{ver}/cli/php.ini",
                     "/usr/local/php/{compact}/etc/php.ini", "/etc/php.ini"],
        "conf_win": [r"{root}\php.ini"],
        "service_nix": ["php{ver}-fpm", "php-fpm-{compact}", "php-fpm"],
        "service_win": [],
        "ctl": {"configtest": "{bin} -t"},
        "unit": "php{ver}-fpm",
        "fpm": True,
    },
    "mysql": {
        "label": "MySQL",
        "exes": ["mysql", "mysqld"],
        "roots_nix": ["/usr/local/mysql", "/usr/local/mysql-*", "/opt/mysql*"],
        "roots_win": [r"C:\Program Files\MySQL", r"C:\Program Files\MariaDB*",
                      r"C:\mysql*", r"C:\ProgramData\MySQL"],
        "conf_nix": ["/etc/my.cnf", "/etc/mysql/my.cnf",
                     "/etc/mysql/mysql.conf.d/mysqld.cnf",
                     "/usr/local/mysql/etc/my.cnf"],
        "conf_win": [r"{root}\my.ini", r"%ProgramData%\MySQL\MySQL Server {ver}\my.ini"],
        "service_nix": ["mysql", "mysqld"],
        "service_win": ["MySQL{compact}", "MySQL", "MySQL80"],
        "ctl": {},
        "unit": "mysql",
        "db": True,
    },
    "mariadb": {
        "label": "MariaDB",
        "exes": ["mariadb", "mariadbd", "mysql"],
        "roots_nix": ["/usr/local/mariadb", "/usr/local/mariadb-*", "/opt/mariadb*"],
        "roots_win": [r"C:\Program Files\MariaDB*", r"C:\mariadb*"],
        "conf_nix": ["/etc/my.cnf", "/etc/mysql/my.cnf",
                     "/etc/mysql/mariadb.conf.d/50-server.cnf"],
        "conf_win": [r"{root}\my.ini"],
        "service_nix": ["mariadb", "mysql"],
        "service_win": ["MariaDB", "MySQL"],
        "ctl": {},
        "unit": "mariadb",
        "db": True,
    },
    "postgres": {
        "label": "PostgreSQL",
        "exes": ["psql", "postgres", "pg_ctl"],
        "roots_nix": ["/usr/lib/postgresql/{ver}", "/usr/local/pgsql", "/opt/pgsql*",
                      "/usr/pgsql-{ver}"],
        "roots_win": [r"C:\Program Files\PostgreSQL\{ver}", r"H:\pgsql*", r"D:\pgsql*",
                      r"C:\pgsql*"],
        "conf_nix": ["/etc/postgresql/{ver}/main/postgresql.conf",
                     "{pgdata}/postgresql.conf"],
        "conf_win": [r"{pgdata}\postgresql.conf"],
        "service_nix": ["postgresql@{ver}-main", "postgresql-{ver}", "postgresql"],
        "service_win": ["postgresql-x64-{ver}", "postgresql-{ver}"],
        "ctl": {"configtest": "{bindir}/pg_ctl -D {pgdata} status"},
        "unit": "postgresql",
        "db": True,
        "hba": True,
    },
    "node": {
        "label": "Node.js",
        "exes": ["node", "nodejs"],
        "roots_nix": ["/usr/local/lib/nodejs", "/usr/local/n", "/opt/nodejs",
                      "~/.nvm/versions/node", "/usr/local/nvm/versions/node"],
        "roots_win": [r"C:\Program Files\nodejs", r"C:\Program Files (x86)\nodejs",
                      r"%APPDATA%\nvm", r"%LOCALAPPDATA%\nvm", r"C:\nodejs"],
        "conf_nix": [], "conf_win": [], "service_nix": [], "service_win": [],
        "ctl": {}, "unit": "",
    },
    "java": {
        "label": "Java",
        "exes": ["java"],
        "roots_nix": ["/usr/lib/jvm", "/opt/java", "/opt/jdk*", "/usr/java/*"],
        "roots_win": [r"C:\Program Files\Java", r"C:\Program Files\Eclipse Adoptium",
                      r"C:\Program Files\Microsoft\jdk*",
                      r"C:\Program Files\Amazon Corretto", r"C:\Program Files\Zulu"],
        "conf_nix": [], "conf_win": [], "service_nix": [], "service_win": [],
        "ctl": {}, "unit": "",
    },
    "python": {
        "label": "Python",
        "exes": ["python", "python3", "py"],
        "roots_nix": ["/usr/local/lib", "/opt/python*", "~/.pyenv/versions",
                      "~/miniconda3", "~/anaconda3"],
        "roots_win": [r"C:\Python*", r"C:\Program Files\Python*",
                      r"%LOCALAPPDATA%\Programs\Python", r"%USERPROFILE%\anaconda3",
                      r"%USERPROFILE%\miniconda3"],
        "conf_nix": [], "conf_win": [], "service_nix": [], "service_win": [],
        "ctl": {}, "unit": "",
    },
    "nginx": {
        "label": "Nginx",
        "exes": ["nginx"],
        "roots_nix": ["/usr/local/nginx", "/opt/nginx*", "/usr/share/nginx"],
        "roots_win": [r"C:\nginx*", r"C:\Program Files\nginx", r"D:\nginx*"],
        "conf_nix": ["/etc/nginx/nginx.conf", "/usr/local/nginx/conf/nginx.conf"],
        "conf_win": [r"{root}\conf\nginx.conf"],
        "service_nix": ["nginx"],
        "service_win": [],                                  # Windows 无原生服务，需 nssm
        "ctl": {"configtest": "{bin} -t", "reload": "{bin} -s reload"},
        "unit": "nginx",
    },
    "redis": {
        "label": "Redis",
        "exes": ["redis-server", "redis-cli"],
        "roots_nix": ["/usr/local/redis", "/opt/redis*", "/usr/local/bin"],
        "roots_win": [r"C:\Redis*", r"C:\Program Files\Redis"],
        "conf_nix": ["/etc/redis/redis.conf", "/usr/local/redis/redis.conf"],
        "conf_win": [r"{root}\redis.windows.conf", r"{root}\redis.conf"],
        "service_nix": ["redis", "redis-server"],
        "service_win": ["Redis"],
        "ctl": {"configtest": "{bin}-cli ping"},
        "unit": "redis",
    },
}

FAMILIES = tuple(FAMILY)


# ══════════════════════════════════════════════════════════
# 兼容层目录（声明式）
# ══════════════════════════════════════════════════════════
# 每条 = 一个版本区间一个层。**同一 family 内顺序即优先级**：具体在前、兜底在后，
# 首个 satisfies() 命中的生效。兜底层 match 用 "*"。
#
# caps 只写**会影响面板行为**的开关（生成配置、拼 SQL、给模板时要不要避开），
# 不追求把发行说明抄全。traits 是人读的差异要点（界面展示用）。
LAYERS = [
    # ── PHP ────────────────────────────────────────────────
    {
        "id": "php5.3-5.5", "family": "php", "match": ">=5.3 <5.6",
        "title": "PHP 5.3 – 5.5",
        "caps": {"namespace": True, "trait": True, "generator": True,
                 "short_array": True, "variadic": False, "scalar_type": False,
                 "return_type": False, "null_coalesce": False, "arrow_func": False,
                 "typed_prop": False, "jit": False, "union_type": False, "enum": False},
        "traits": ["无标量类型声明与返回类型（PHP 7 才有）",
                   "无 `??` / `<=>`；给模板时不能用",
                   "5.4 起移除 register_globals / safe_mode / magic_quotes",
                   "ext/mysql 仍在（PHP 7 起移除）"],
    },
    {
        "id": "php5.6", "family": "php", "match": "5.6",
        "title": "PHP 5.6",
        "caps": {"namespace": True, "trait": True, "generator": True,
                 "short_array": True, "variadic": True, "scalar_type": False,
                 "return_type": False, "null_coalesce": False, "arrow_func": False,
                 "typed_prop": False, "jit": False, "union_type": False, "enum": False},
        "traits": ["引入变长参数 `...`、常量表达式",
                   "仍是 EOL 版本，安全通告只走发行版回补",
                   "`ext/mysql` 已废弃，模板应改用 mysqli / PDO"],
    },
    {
        "id": "php7.0-7.3", "family": "php", "match": ">=7.0 <7.4",
        "title": "PHP 7.0 – 7.3",
        "caps": {"namespace": True, "trait": True, "generator": True,
                 "short_array": True, "variadic": True, "scalar_type": True,
                 "return_type": True, "null_coalesce": True, "arrow_func": False,
                 "typed_prop": False, "jit": False, "union_type": False, "enum": False},
        "traits": ["7.0 移除 ext/mysql、ereg、split()；`??` `<=>` 可用",
                   "7.1 可空类型 `?T`、多 catch",
                   "7.3 灵活 Heredoc、`list()` 引用赋值",
                   "给站点写 ini 时 opcache 已内置"],
    },
    {
        "id": "php7.4", "family": "php", "match": "7.4",
        "title": "PHP 7.4",
        "caps": {"namespace": True, "trait": True, "generator": True,
                 "short_array": True, "variadic": True, "scalar_type": True,
                 "return_type": True, "null_coalesce": True, "arrow_func": True,
                 "typed_prop": True, "jit": False, "union_type": False, "enum": False},
        "traits": ["箭头函数 `fn()`、类型化属性、`??=`",
                   "7.4 是 7.x 的终点，很多老 CMS 仍把它作为上限"],
    },
    {
        "id": "php8.0-8.1", "family": "php", "match": ">=8.0 <8.2",
        "title": "PHP 8.0 / 8.1",
        "caps": {"namespace": True, "trait": True, "generator": True,
                 "short_array": True, "variadic": True, "scalar_type": True,
                 "return_type": True, "null_coalesce": True, "arrow_func": True,
                 "typed_prop": True, "jit": True, "union_type": True, "enum": True},
        "traits": ["8.0：JIT、命名参数、构造器属性提升、`match`、联合类型；"
                   "移除 `each()` / `create_function()`，`@` 不再吞致命错误",
                   "8.1：枚举、只读属性、Fiber、`never` 返回类型",
                   "从 5/7 迁上来的老代码在此层最容易炸，部署前务必先跑语法检查"],
    },
    {
        "id": "php8.2-8.3", "family": "php", "match": ">=8.2 <8.4",
        "title": "PHP 8.2 / 8.3",
        "caps": {"namespace": True, "trait": True, "generator": True,
                 "short_array": True, "variadic": True, "scalar_type": True,
                 "return_type": True, "null_coalesce": True, "arrow_func": True,
                 "typed_prop": True, "jit": True, "union_type": True, "enum": True},
        "traits": ["8.2：只读类、DNF 类型、动态属性废弃（`#[AllowDynamicProperties]`）",
                   "8.3：类型化类常量、`json_validate()`、Override 属性",
                   "给站点生成配置时 `display_errors` 默认已改为不显示"],
    },
    {
        "id": "php8.4+", "family": "php", "match": ">=8.4",
        "title": "PHP 8.4 及以后",
        "caps": {"namespace": True, "trait": True, "generator": True,
                 "short_array": True, "variadic": True, "scalar_type": True,
                 "return_type": True, "null_coalesce": True, "arrow_func": True,
                 "typed_prop": True, "jit": True, "union_type": True, "enum": True,
                 "property_hook": True},
        "traits": ["属性钩子（property hooks）、非对称可见性",
                   "`E_STRICT` 等老常量继续移除，老模板会报 undefined constant",
                   "新版本发布后先在本层验证，再决定是否收进稳定模板"],
    },
    # ── MySQL ──────────────────────────────────────────────
    {
        "id": "mysql5.5-5.6", "family": "mysql", "match": ">=5.5 <5.7",
        "title": "MySQL 5.5 / 5.6",
        "caps": {"json_type": False, "generated_col": False, "cte": False,
                 "window_func": False, "atomic_ddl": False, "query_cache": True,
                 "grant_creates_user": True, "default_auth": "mysql_native_password",
                 "charset_default": "utf8mb4_general_ci",
                 "no_auto_create_user_removed": False},
        "traits": ["`GRANT ... IDENTIFIED BY` 会连带建用户：建用户与授权可以一条完成",
                   "有查询缓存（`query_cache_size`）",
                   "无 JSON 类型、无生成列、无窗口函数 / CTE"],
    },
    {
        "id": "mysql5.7", "family": "mysql", "match": ">=5.7 <8.0",
        "title": "MySQL 5.7",
        "caps": {"json_type": True, "generated_col": True, "cte": False,
                 "window_func": False, "atomic_ddl": False, "query_cache": True,
                 "grant_creates_user": False, "default_auth": "mysql_native_password",
                 "charset_default": "utf8mb4_general_ci",
                 "no_auto_create_user_removed": False},
        "traits": ["引入 JSON 列类型与生成列",
                   "默认 `sql_mode` 含 STRICT_TRANS_TABLES + ONLY_FULL_GROUP_BY，"
                   "老应用的宽松散写入会直接报错",
                   "`CREATE USER` 与 `GRANT` 已解耦（不再能靠 GRANT 隐式建用户）"],
    },
    {
        "id": "mysql8.0-8.3", "family": "mysql", "match": ">=8.0 <8.4",
        "title": "MySQL 8.0 – 8.3",
        "caps": {"json_type": True, "generated_col": True, "cte": True,
                 "window_func": True, "atomic_ddl": True, "query_cache": False,
                 "grant_creates_user": False, "default_auth": "caching_sha2_password",
                 "charset_default": "utf8mb4_0900_ai_ci",
                 "no_auto_create_user_removed": True},
        "traits": ["默认认证插件改为 caching_sha2_password —— 老客户端（旧 PHP 的 mysqli / "
                   "旧版 Navicat）连不上时，需要显式指定 mysql_native_password 或升级客户端",
                   "查询缓存被移除；`NO_AUTO_CREATE_USER` 模式被移除",
                   "支持窗口函数与 CTE；DDL 原子化",
                   "默认排序规则为 utf8mb4_0900_ai_ci"],
    },
    {
        "id": "mysql8.4+", "family": "mysql", "match": ">=8.4",
        "title": "MySQL 8.4 及以后",
        "caps": {"json_type": True, "generated_col": True, "cte": True,
                 "window_func": True, "atomic_ddl": True, "query_cache": False,
                 "grant_creates_user": False, "default_auth": "caching_sha2_password",
                 "charset_default": "utf8mb4_0900_ai_ci",
                 "no_auto_create_user_removed": True, "native_auth_disabled": True},
        "traits": ["mysql_native_password 默认被禁用：必须显式 "
                   "`INSTALL COMPONENT` / 启动参数才能重新启用",
                   "给老应用建库时优先直接用 caching_sha2_password，别再回退老插件"],
    },
    # ── MariaDB ────────────────────────────────────────────
    {
        "id": "mariadb10.0-10.4", "family": "mariadb", "match": ">=10.0 <10.5",
        "title": "MariaDB 10.0 – 10.4",
        "caps": {"json_type": False, "cte": False, "window_func": False,
                 "default_auth": "mysql_native_password",
                 "charset_default": "utf8mb4_general_ci", "grant_creates_user": True},
        "traits": ["JSON 是 LONGTEXT 别名而非真类型",
                   "窗口函数 10.2 起才有，CTE 10.2 起才有",
                   "与 MySQL 5.7 高度兼容，但不能套用 MySQL 8.0 的认证/字符集默认值"],
    },
    {
        "id": "mariadb10.5+", "family": "mariadb", "match": ">=10.5",
        "title": "MariaDB 10.5 及以后",
        "caps": {"json_type": False, "cte": True, "window_func": True,
                 "default_auth": "mysql_native_password",
                 "charset_default": "utf8mb4_general_ci", "grant_creates_user": True},
        "traits": ["默认 root 走 unix_socket 认证（本地 socket 免密，TCP 连不上属正常）",
                   "排序规则默认仍是 utf8mb4_general_ci，与 MySQL 8 不同，迁移时注意"],
    },
    # ── PostgreSQL ─────────────────────────────────────────
    {
        "id": "pg9.x", "family": "postgres", "match": ">=9.0 <10.0",
        "title": "PostgreSQL 9.x",
        "caps": {"identity_col": False, "logical_repl": False, "scram": False,
                 "recovery_conf": True, "public_schema_create": True,
                 "merge": False, "stats_io": False},
        "traits": ["`recovery.conf` 还在（12 起被移除，改用 standby.signal）",
                   "认证默认 md5/trust，尚无 SCRAM",
                   "无标识列（IDENTITY），用 serial + sequence"],
    },
    {
        "id": "pg10-11", "family": "postgres", "match": ">=10 <12",
        "title": "PostgreSQL 10 / 11",
        "caps": {"identity_col": True, "logical_repl": True, "scram": True,
                 "recovery_conf": True, "public_schema_create": True,
                 "merge": False, "stats_io": False},
        "traits": ["引入声明式分区与标识列、内置逻辑复制",
                   "SCRAM-SHA-256 认证可用（14 起成为默认）",
                   "建库后 public 模式仍默认对所有人可写"],
    },
    {
        "id": "pg12-14", "family": "postgres", "match": ">=12 <15",
        "title": "PostgreSQL 12 – 14",
        "caps": {"identity_col": True, "logical_repl": True, "scram": True,
                 "recovery_conf": False, "public_schema_create": True,
                 "merge": False, "stats_io": False},
        "traits": ["12 移除 recovery.conf，备库改用 standby.signal + postgresql.auto.conf",
                   "12 起生成列（STORED）可用；`COPY ... WHERE` 可用",
                   "public 模式**仍**默认允许所有用户建对象"],
    },
    {
        "id": "pg15-17", "family": "postgres", "match": ">=15 <18",
        "title": "PostgreSQL 15 – 17",
        "caps": {"identity_col": True, "logical_repl": True, "scram": True,
                 "recovery_conf": False, "public_schema_create": False,
                 "merge": True, "stats_io": True},
        "traits": ["⚠️ 15 起 public 模式**不再**默认授予 CREATE 权限 —— "
                   "新建普通用户在 public 下建表会 permission denied，"
                   "需要显式 `GRANT CREATE ON SCHEMA public` 或把对象建在自己模式里",
                   "15 引入 MERGE 语句与 pg_stat_io"],
    },
    {
        "id": "pg18+", "family": "postgres", "match": ">=18",
        "title": "PostgreSQL 18 及以后",
        "caps": {"identity_col": True, "logical_repl": True, "scram": True,
                 "recovery_conf": False, "public_schema_create": False,
                 "merge": True, "stats_io": True},
        "traits": ["继承 15+ 的 public 模式收紧策略",
                   "新版本先在本层验证再收进稳定模板"],
    },
    # ── Node.js ────────────────────────────────────────────
    {
        "id": "node<=14", "family": "node", "match": "<=14",
        "title": "Node.js 14 及更早",
        "caps": {"esm": True, "global_fetch": False, "test_runner": False,
                 "watch_mode": False, "node_prefix": False, "env_file": False,
                 "require_esm": False},
        "traits": ["无全局 fetch，需 undici / node-fetch",
                   "无内置测试运行器与 watch 模式"],
    },
    {
        "id": "node16-17", "family": "node", "match": ">=16 <18",
        "title": "Node.js 16 / 17",
        "caps": {"esm": True, "global_fetch": False, "test_runner": False,
                 "watch_mode": False, "node_prefix": True, "env_file": False,
                 "require_esm": False},
        "traits": ["支持 `node:` 前缀导入内置模块",
                   "全局 fetch 还没有（18 起才有）"],
    },
    {
        "id": "node18-19", "family": "node", "match": ">=18 <20",
        "title": "Node.js 18 / 19",
        "caps": {"esm": True, "global_fetch": True, "test_runner": True,
                 "watch_mode": True, "node_prefix": True, "env_file": False,
                 "require_esm": False},
        "traits": ["全局 fetch 与内置测试运行器登场（18 起步、20 转正）",
                   "`--watch` 可用；OpenSSL 3 导致老 native 模块需要重编译"],
    },
    {
        "id": "node20-21", "family": "node", "match": ">=20 <22",
        "title": "Node.js 20 / 21",
        "caps": {"esm": True, "global_fetch": True, "test_runner": True,
                 "watch_mode": True, "node_prefix": True, "env_file": True,
                 "require_esm": False},
        "traits": ["`--env-file` 可直接读 .env（不必再装 dotenv）",
                   "测试运行器转正；给站点写启动命令可以带 --env-file"],
    },
    {
        "id": "node22+", "family": "node", "match": ">=22",
        "title": "Node.js 22 及以后",
        "caps": {"esm": True, "global_fetch": True, "test_runner": True,
                 "watch_mode": True, "node_prefix": True, "env_file": True,
                 "require_esm": True},
        "traits": ["`require()` 可以加载 ESM（22.12 起），老 CommonJS 工程兼容性更好",
                   "LTS 主线，新建站点默认推荐"],
    },
    # ── Java ───────────────────────────────────────────────
    {
        "id": "java8", "family": "java", "match": "1.8 | 8",
        "title": "Java 8",
        "caps": {"modules": False, "var": False, "switch_expr": False,
                 "text_block": False, "record": False, "sealed": False,
                 "virtual_thread": False, "maxram_percent": False},
        "traits": ["无模块系统（9 才有）、无 `var`、无文本块",
                   "默认 GC 为 Parallel；`-XX:MaxRAMPercentage` 不可用，"
                   "容器里要么显式 -Xmx 要么打 UseContainerSupport 补丁",
                   "仍是大量老系统（含 Minecraft 服务端）的运行时"],
    },
    {
        "id": "java9-10", "family": "java", "match": ">=9 <11",
        "title": "Java 9 / 10",
        "caps": {"modules": True, "var": True, "switch_expr": False,
                 "text_block": False, "record": False, "sealed": False,
                 "virtual_thread": False, "maxram_percent": True},
        "traits": ["模块系统（JPMS）落地：classpath 上的老 jar 可能需 --add-opens",
                   "默认 GC 改为 G1（9 起）"],
    },
    {
        "id": "java11", "family": "java", "match": ">=11 <17",
        "title": "Java 11 / 12 – 16",
        "caps": {"modules": True, "var": True, "switch_expr": True,
                 "text_block": True, "record": False, "sealed": False,
                 "virtual_thread": False, "maxram_percent": True},
        "traits": ["LTS（11）：内置 HttpClient 转正，Nashorn 被移除",
                   "14 起 switch 表达式、15 起文本块；16 起 record（正式）"],
    },
    {
        "id": "java17-20", "family": "java", "match": ">=17 <21",
        "title": "Java 17 – 20",
        "caps": {"modules": True, "var": True, "switch_expr": True,
                 "text_block": True, "record": True, "sealed": True,
                 "virtual_thread": False, "maxram_percent": True},
        "traits": ["LTS（17）：record / sealed / 模式匹配逐步转正",
                   "强封装 JDK 内部 API（`--illegal-access` 失效），老反射代码需 --add-opens"],
    },
    {
        "id": "java21+", "family": "java", "match": ">=21",
        "title": "Java 21 及以后",
        "caps": {"modules": True, "var": True, "switch_expr": True,
                 "text_block": True, "record": True, "sealed": True,
                 "virtual_thread": True, "maxram_percent": True},
        "traits": ["LTS（21）：虚拟线程、模式匹配 for switch 转正、分代 ZGC",
                   "给服务端起进程时可用 `-XX:+UseZGC` 与虚拟线程提升并发"],
    },
    # ── Python ─────────────────────────────────────────────
    {
        "id": "python2", "family": "python", "match": ">=2.0 <3.0",
        "title": "Python 2.x",
        "caps": {"fstring": False, "dataclass": False, "walrus": False,
                 "match_stmt": False, "tomllib": False, "venv_module": False,
                 "free_threading": False},
        "traits": ["语法与 3.x 不兼容：`print` 是语句、字符串默认 bytes",
                   "已彻底 EOL，只做清点，不生成新配置"],
    },
    {
        "id": "python3.0-3.5", "family": "python", "match": ">=3.0 <3.6",
        "title": "Python 3.0 – 3.5",
        "caps": {"fstring": False, "dataclass": False, "walrus": False,
                 "match_stmt": False, "tomllib": False, "venv_module": True,
                 "free_threading": False},
        "traits": ["无 f-string（3.6 才有）、无 dataclasses（3.7 才有）",
                   "venv 自 3.3 可用"],
    },
    {
        "id": "python3.6-3.7", "family": "python", "match": ">=3.6 <3.8",
        "title": "Python 3.6 / 3.7",
        "caps": {"fstring": True, "dataclass": True, "walrus": False,
                 "match_stmt": False, "tomllib": False, "venv_module": True,
                 "free_threading": False},
        "traits": ["f-string（3.6）、dataclasses（3.7）",
                   "3.7 字典保持插入序成为语言保证"],
    },
    {
        "id": "python3.8-3.9", "family": "python", "match": ">=3.8 <3.10",
        "title": "Python 3.8 / 3.9",
        "caps": {"fstring": True, "dataclass": True, "walrus": True,
                 "match_stmt": False, "tomllib": False, "venv_module": True,
                 "free_threading": False},
        "traits": ["海象运算符 `:=`、仅位置参数 `/`",
                   "3.9 起内置泛型 `list[int]`（不必再 from typing import List）"],
    },
    {
        "id": "python3.10-3.11", "family": "python", "match": ">=3.10 <3.12",
        "title": "Python 3.10 / 3.11",
        "caps": {"fstring": True, "dataclass": True, "walrus": True,
                 "match_stmt": True, "tomllib": True, "venv_module": True,
                 "free_threading": False},
        "traits": ["结构化模式匹配 `match`（3.10）",
                   "3.11 起 `tomllib` 进标准库（此前要装 tomli）、异常组 `except*`",
                   "3.11 解释器提速明显，是当前多数项目的稳妥下限"],
    },
    {
        "id": "python3.12", "family": "python", "match": "3.12",
        "title": "Python 3.12",
        "caps": {"fstring": True, "dataclass": True, "walrus": True,
                 "match_stmt": True, "tomllib": True, "venv_module": True,
                 "free_threading": False, "distutils": False},
        "traits": ["移除 `distutils` —— 依赖它的老构建脚本（部分老 setuptools 用法）会断",
                   "移除 `asynchat` / `asyncore` / `smtpd`（用它们的邮件/网络脚本需改写）",
                   "PEP 701：f-string 内可复用引号与换行"],
    },
    {
        "id": "python3.13+", "family": "python", "match": ">=3.13",
        "title": "Python 3.13 及以后",
        "caps": {"fstring": True, "dataclass": True, "walrus": True,
                 "match_stmt": True, "tomllib": True, "venv_module": True,
                 "free_threading": True, "distutils": False},
        "traits": ["自由线程（无 GIL）构建是可选项，默认构建仍带 GIL",
                   "实验性 JIT；`ensurepip` 在部分自制构建里可能不可用，"
                   "装依赖前先确认裸 venv 能不能建起来"],
    },
    # ── Nginx ──────────────────────────────────────────────
    {
        "id": "nginx<=1.24", "family": "nginx", "match": "<1.25",
        "title": "Nginx 1.24 及更早",
        "caps": {"http2": True, "http3": False, "stream": True, "dump_config": True},
        "traits": ["HTTP/2 用 `listen ... http2` 写法",
                   "HTTP/3/QUIC 需自编译带 quic 补丁"],
    },
    {
        "id": "nginx1.25+", "family": "nginx", "match": ">=1.25",
        "title": "Nginx 1.25 及以后",
        "caps": {"http2": True, "http3": True, "stream": True, "dump_config": True},
        "traits": ["HTTP/3 内置，用 `listen ... quic` + `http3 on`",
                   "1.25.1 起 `http2` 独立成指令，旧的 `listen ... http2` 写法仍兼容但已过时"],
    },
    # ── Redis ──────────────────────────────────────────────
    {
        "id": "redis<=5", "family": "redis", "match": "<=5",
        "title": "Redis 5 及更早",
        "caps": {"acl": False, "io_threads": False, "functions": False, "resp3": False},
        "traits": ["只有 requirepass 单密码，无 ACL 多用户",
                   "无多线程 I/O"],
    },
    {
        "id": "redis6", "family": "redis", "match": ">=6 <7",
        "title": "Redis 6",
        "caps": {"acl": True, "io_threads": True, "functions": False, "resp3": True},
        "traits": ["引入 ACL（多用户 + 细粒度权限）、RESP3、多线程 I/O",
                   "`redis-cli --no-auth-warning` 可用于脚本"],
    },
    {
        "id": "redis7+", "family": "redis", "match": ">=7",
        "title": "Redis 7 及以后",
        "caps": {"acl": True, "io_threads": True, "functions": True, "resp3": True},
        "traits": ["引入 Functions（服务端脚本，替代部分 EVAL 用法）",
                   "分片集群改走 cluster shard 管理指令"],
    },
]


# ══════════════════════════════════════════════════════════
# 门面 API
# ══════════════════════════════════════════════════════════
def knows(family: str) -> bool:
    """是否是已登记的家族。不是就别给它归层（npm 这类工具不是运行时家族）。"""
    return str(family or "").strip().lower() in FAMILY


def layers(family: str = "") -> list:
    """列出兼容层。给了 family 就只列那一族（按优先级序）。"""
    fam = str(family or "").strip().lower()
    return [dict(x) for x in LAYERS if not fam or x["family"] == fam]


def families() -> list:
    """已知家族 + 各自有几层（界面用）。"""
    out = []
    for f in FAMILIES:
        info = FAMILY[f]
        out.append({"family": f, "label": info["label"], "layers": len(layers(f)),
                    "exes": list(info["exes"])})
    return out


def resolve(family: str, version: str) -> dict:
    """**核心入口**：给 (家族, 版本) → 命中的兼容层。

    永不返回 None：没有更具体的层命中时落到该族兜底层；连兜底层都没有
    （未知家族）则返回一个通用的「未知版本」层，而不是抛异常 ——
    面板遇到没见过的版本应该照常显示，不能因为清点不出来就白屏。
    """
    fam = str(family or "").strip().lower()
    ver = str(version or "").strip()
    for lay in LAYERS:
        if lay["family"] != fam:
            continue
        if satisfies(ver, lay.get("match", "")):
            return _decorate(lay, fam, ver)
    return _decorate({
        "id": f"{fam or 'unknown'}:unknown", "family": fam, "match": "*",
        "title": f"{FAMILY.get(fam, {}).get('label', fam or '未知')} 未收录版本",
        "caps": {}, "traits": ["该版本未在兼容层目录里登记 —— "
                               "面板按通用方式处理；建议在此补一条声明"],
    }, fam, ver)


def _decorate(lay: dict, fam: str, ver: str) -> dict:
    """补上 family 级共享事实，并把路径里的占位符展开。"""
    out = dict(lay)
    info = FAMILY.get(fam, {})
    out["label"] = info.get("label", fam)
    out["version"] = ver
    out["compact"] = compact(ver) if ver else ""
    out["caps"] = dict(lay.get("caps") or {})
    out["traits"] = list(lay.get("traits") or [])
    return out


def explain(family: str, version: str) -> dict:
    """给界面看的一份说明：命中的层 + 相对**上一层**多了/少了什么能力。

    这正是「一个版本一个兼容层」想要的效果 —— 运维不必背版本差异，
    面板直接说「你这台是 8.4，比 8.0 少了什么、要注意什么」。
    """
    lay = resolve(family, version)
    fam = lay["family"]
    same = [x for x in LAYERS if x["family"] == fam]
    idx = next((i for i, x in enumerate(same) if x["id"] == lay["id"]), -1)
    added, removed, prev_title = [], [], ""
    if idx > 0:                                   # 与**更旧**的那层比
        prev = same[idx - 1]
        prev_title = prev.get("title", "")
        pc, cc = dict(prev.get("caps") or {}), dict(lay.get("caps") or {})
        for k, v in cc.items():
            if k not in pc:
                added.append(f"{k}={v}" if not isinstance(v, bool) else k)
            elif pc[k] != v:
                added.append(f"{k}: {pc[k]} → {v}")
        for k, v in pc.items():
            if k not in cc:
                removed.append(k)
    return {
        "family": fam, "label": lay.get("label", fam), "version": lay["version"],
        "layer": lay["id"], "title": lay.get("title", ""),
        "match": lay.get("match", ""), "caps": lay["caps"],
        "traits": lay["traits"], "compared_to": prev_title,
        "gained": sorted(added), "dropped": sorted(removed),
        "info": service_info(fam, version),
    }


# ══════════════════════════════════════════════════════════
# 从层里派生出来的实用视图
# ══════════════════════════════════════════════════════════
def _fill(tpl: str, fam: str, ver: str, root: str = "", pgdata: str = "",
          bindir: str = "") -> str:
    """展开路径/服务名模板里的占位符。"""
    return (str(tpl)
            .replace("{ver}", str(ver or ""))
            .replace("{compact}", compact(ver))
            .replace("{root}", str(root or ""))
            .replace("{bindir}", str(bindir or ""))
            .replace("{pgdata}", str(pgdata or "")))


def expand(patterns, *, only_dirs: bool = True) -> list:
    """把候选路径里的 ``~`` / ``%VAR%`` / ``$VAR`` / 通配展开成真实存在的路径。

    这是**唯一**一份展开实现：`roots()` 返回的是「写法」，要拿去扫描就得先过这里。
    去重按大小写不敏感的绝对路径，保序。
    """
    import glob as _glob
    import os
    out = []
    for raw in (patterns or ()):
        pat = os.path.expandvars(os.path.expanduser(str(raw)))
        if "*" in pat or "?" in pat:
            hits = _glob.glob(pat)
        else:
            hits = [pat]
        for h in hits:
            if only_dirs and not os.path.isdir(h):
                continue
            out.append(h)
    seen, uniq = set(), []
    for d in out:
        k = os.path.normcase(os.path.abspath(d))
        if k not in seen:
            seen.add(k)
            uniq.append(d)
    return uniq


def roots(family: str, platform: str = "") -> list:
    """该家族**约定俗成**的安装根候选（多版本清点用）。

    只列上游安装器与各发行版的真实默认位置；不做全盘扫描。
    """
    info = FAMILY.get(str(family or "").lower(), {})
    plat = (platform or "").lower()
    if not plat:
        import sys
        plat = "win" if sys.platform.startswith("win") else "nix"
    key = "roots_win" if plat.startswith("win") else "roots_nix"
    return list(info.get(key, []))


def config_candidates(family: str, version: str, platform: str = "",
                      root: str = "", pgdata: str = "") -> list:
    """该版本**配置文件**的候选位置（只读探测用，不写）。"""
    info = FAMILY.get(str(family or "").lower(), {})
    plat = (platform or "").lower()
    if not plat:
        import sys
        plat = "win" if sys.platform.startswith("win") else "nix"
    key = "conf_win" if plat.startswith("win") else "conf_nix"
    import os
    out = []
    for t in info.get(key, []):
        p = os.path.expandvars(os.path.expanduser(_fill(t, family, version, root, pgdata)))
        out.append(p)
    return out


def service_names(family: str, version: str) -> dict:
    """该版本的**服务名候选**（各发行版/安装器取名不统一，只能逐个试）。"""
    info = FAMILY.get(str(family or "").lower(), {})

    def _dedup(items):
        seen, out = set(), []
        for x in items:
            if x and x not in seen:
                seen.add(x); out.append(x)
        return out

    return {
        "nix": _dedup(_fill(t, family, version) for t in info.get("service_nix", [])),
        "win": _dedup(_fill(t, family, version) for t in info.get("service_win", [])),
        "unit": _fill(info.get("unit", ""), family, version),
    }


def ctl_commands(family: str, version: str, *, bin: str = "", pgdata: str = "",
                 bindir: str = "") -> dict:
    """该版本的**校验/重载**命令模板。

    :param bin: 可执行文件全路径（模板里的 ``{bin}``）
    :param bindir: 可执行文件所在目录（模板里的 ``{bindir}``，如 pg_ctl 这类
        「必须从 bin 目录取工具」的场景）
    :param pgdata: PostgreSQL 数据目录（模板里的 ``{pgdata}``）
    """
    info = FAMILY.get(str(family or "").lower(), {})
    out = {}
    for act, tpl in (info.get("ctl") or {}).items():
        cmd = _fill(tpl, family, version, pgdata=pgdata, bindir=bindir)
        out[act] = cmd.replace("{bin}", str(bin or ""))
    return out


def service_info(family: str, version: str, *, bin: str = "", bindir: str = "") -> dict:
    """给界面/排障用的一览：命中层 + 可执行名 + 根候选 + 配置候选 + 服务名。"""
    lay = resolve(family, version)
    return {
        "family": lay["family"], "layer": lay["id"], "title": lay.get("title", ""),
        "exes": list(FAMILY.get(lay["family"], {}).get("exes", [])),
        "roots": roots(lay["family"]),
        "configs": config_candidates(lay["family"], version),
        "services": service_names(lay["family"], version),
        "ctl": ctl_commands(lay["family"], version, bin=bin, bindir=bindir),
    }


def php_fastcgi(version: str, platform: str = "") -> str:
    """PHP 版本 → Nginx ``fastcgi_pass`` 端点（**面板自己的约定**，不采用任何商业面板的路径）。

    这里的地址是「站点想用哪个 PHP 版本」时面板写进 Nginx 配置的地方；
    php-fpm 需要各自监听在该端点（per-version php-fpm 池的托管是独立任务，
    本函数只负责把版本映射成约定的端点，保证配置正确、可复用）。

    - Windows：``127.0.0.1:90{MM}``（MM = 主版本×10 + 次版本；8.2 → 9082，7.4 → 9074）
    - Linux：``unix:/run/zpanel/php/php{主.次}-fpm.sock``（8.2 → php8.2-fpm.sock）
    """
    plat = (platform or "").lower()
    if not plat:
        import sys
        plat = "win" if sys.platform.startswith("win") else "nix"
    m = _NUM.match(str(version or ""))
    maj = int(m.group(1)) if (m and m.group(1)) else 0
    mnr = int(m.group(2)) if (m and m.group(2)) else 0
    if plat.startswith("win"):
        return f"127.0.0.1:{9000 + maj * 10 + mnr}" if (maj or mnr) else "127.0.0.1:9000"
    return f"unix:/run/zpanel/php/php{maj}.{mnr}-fpm.sock" if (maj or mnr) else \
        "unix:/run/zpanel/php/php-fpm.sock"


# ══════════════════════════════════════════════════════════
# 数据库档案：把「按版本拼 SQL」从调用方挪到这里
# ══════════════════════════════════════════════════════════
def db_profile(family: str, version: str) -> dict:
    """数据库的版本相关 SQL 事实。

    调用方（database 扩展 / dbclient）不必再写 `if version >= (8,0): ...`，
    直接读这里的字段决定怎么拼语句。
    """
    fam = str(family or "").strip().lower()
    lay = resolve(fam, version)
    caps = lay["caps"]
    prof = {
        "family": fam, "version": version, "layer": lay["id"],
        "default_auth": caps.get("default_auth", ""),
        "charset_default": caps.get("charset_default", ""),
        "supports_cte": bool(caps.get("cte")),
        "supports_window": bool(caps.get("window_func")),
        "supports_json": bool(caps.get("json_type")),
        "grant_creates_user": bool(caps.get("grant_creates_user")),
        "native_auth_disabled": bool(caps.get("native_auth_disabled")),
        "public_schema_create": caps.get("public_schema_create"),
        "notes": [],
    }
    # 建用户语句：老版本可以一条 GRANT 带密码，新版本必须先 CREATE USER
    if prof["grant_creates_user"]:
        prof["create_user_strategy"] = "grant_with_password"
        prof["notes"].append("该版本 `GRANT ... IDENTIFIED BY` 会连带建用户；"
                             "但为统一行为，仍建议先显式 CREATE USER")
    else:
        prof["create_user_strategy"] = "create_user_then_grant"
    if prof["default_auth"]:
        prof["notes"].append(f"默认认证插件：{prof['default_auth']}")
    if prof["native_auth_disabled"]:
        prof["notes"].append("mysql_native_password 默认禁用：老客户端需升级，"
                             "或显式启用该组件")
    if prof["public_schema_create"] is False:
        prof["notes"].append("public 模式默认不授予 CREATE：普通用户建表前"
                             "需 GRANT CREATE ON SCHEMA public，或建自己的模式")
    if fam == "mariadb":
        prof["notes"].append("MariaDB 的排序规则/认证默认值与同代 MySQL 不同，"
                             "迁移时不要照搬 MySQL 的默认值")
    return prof


def _selftest() -> dict:
    """自检：规约解析、兜底、按版本分层是否真的分开。"""
    cases = [
        ("php", "5.6", "php5.6"), ("php", "7.4.33", "php7.4"),
        ("php", "8.1.2", "php8.0-8.1"), ("php", "8.4.0", "php8.4+"),
        ("mysql", "5.7.44", "mysql5.7"), ("mysql", "8.0.36", "mysql8.0-8.3"),
        ("mysql", "8.4.2", "mysql8.4+"), ("postgres", "18.6", "pg18+"),
        ("postgres", "14.11", "pg12-14"), ("node", "22.11.0", "node22+"),
        ("java", "17.0.9", "java17-20"), ("java", "1.8.0_392", "java8"),
        ("python", "3.13.12", "python3.13+"), ("python", "3.14.5", "python3.13+"),
        ("nginx", "1.24.0", "nginx<=1.24"), ("redis", "7.2.4", "redis7+"),
    ]
    bad = []
    for fam, ver, want in cases:
        got = resolve(fam, ver)["id"]
        if got != want:
            bad.append(f"{fam}/{ver} → {got}（期望 {want}）")
    caps_check = [
        # MySQL 8 的认证默认必须与 5.7 不同，这条错了整个「按版本分层」就没意义
        (db_profile("mysql", "5.7.44")["default_auth"] == "mysql_native_password"),
        (db_profile("mysql", "8.0.36")["default_auth"] == "caching_sha2_password"),
        # PG 15 起 public 模式收紧
        (db_profile("postgres", "14.2")["public_schema_create"] is True),
        (db_profile("postgres", "15.0")["public_schema_create"] is False),
        # PHP 7/8 语法能力分界
        (resolve("php", "7.3")["caps"].get("arrow_func") is False),
        (resolve("php", "7.4")["caps"].get("arrow_func") is True),
        (resolve("php", "5.6")["caps"].get("null_coalesce") is False),
        (resolve("php", "7.0")["caps"].get("null_coalesce") is True),
        # 规约语法本身：或 / 且 / 通配 / 加号
        (satisfies("1.8.0_392", "1.8 | 8") is True),
        (satisfies("8.0.1", "1.8 | 8") is True),     # Java 主版本 8 就该命中 java8 层
        (satisfies("11.0.2", "1.8 | 8") is False),   # 或-分组不得跨主版本误伤
        (satisfies("8.1.0", ">=8.0 <8.2") is True),
        (satisfies("8.2.0", ">=8.0 <8.2") is False),
        (satisfies("7.40.1", "7.4") is False),      # 前缀不能误伤 7.40
        (satisfies("7.4.33", "7.4") is True),
        (satisfies("anything", "*") is True),
        # 模板占位符必须真的展开，不能漏着 {bindir} 出去
        ("{bindir}" not in ctl_commands("postgres", "18", bin="/x/pg_ctl",
                                        bindir="/x")["configtest"]),
        (service_names("mysql", "8.0")["win"] == ["MySQL80", "MySQL"]),
        # 版本 → fastcgi 端点（面板自己的约定，非宝塔路径）
        (php_fastcgi("8.2", "nix").endswith("php8.2-fpm.sock")),
        (php_fastcgi("8.2", "win") == "127.0.0.1:9082"),
        (php_fastcgi("7.4", "win") == "127.0.0.1:9074"),
        (php_fastcgi("8.1", "nix").endswith("php8.1-fpm.sock")),
    ]
    return {"cases": len(cases), "failed": bad,
            "caps_ok": all(caps_check), "ok": (not bad) and all(caps_check)}
