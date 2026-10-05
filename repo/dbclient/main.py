# -*- coding: utf-8 -*-
"""
dbclient — 数据库客户端调用（零框架依赖，机制包）

**不内置数据库，也不实现任何数据库协议。** 只做一件事：安全地调用系统上
**已经装好**的官方客户端程序（`mysql` / `psql` / `redis-cli`）。

## 为什么凭据不能进命令行

`mysql -u root -pS3cr3t` 这种写法，密码会出现在进程命令行里 —— 同机的任何用户
`ps aux`（Windows 上任务管理器 / wmic）都能看到。运维面板必须避开这个坑：

| 数据库 | 安全传密方式 |
| --- | --- |
| MySQL / MariaDB | 临时 `--defaults-extra-file`（0600，用完立即删） |
| PostgreSQL | 环境变量 `PGPASSWORD` |
| Redis | 环境变量 `REDISCLI_AUTH` |
| MongoDB | **不支持** —— 密码只能进 argv 或连接串，宁可不做也不做不安全的 |

（环境变量同样可能被同用户读到，但不会泄漏给其它用户，这是可接受的权衡；
命令行则是公开可见的，不可接受。）

## 依赖

`procs`（执行子进程）。取不到就明确报错，不静默降级。
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time

import zkg

__version__ = "1.0.0"


def _procs():
    mod = zkg.tool("procs")
    if mod is None:
        raise RuntimeError(
            "dbclient 依赖 procs 机制包，但未被加载 —— "
            "检查 manifest.toml 的 dependencies 与 zkg 扫描根是否包含 repo/")
    return mod


# ── 驱动定义 ────────────────────────────────────────────────
# exe：候选可执行文件名（按序查找）；version_args：取版本的参数
DRIVERS = {
    "mysql": {
        "label": "MySQL / MariaDB",
        "exe": ("mysql", "mariadb"),
        "server": ("mysqld", "mariadbd"),
        "version_args": ("--version",),
        "default_port": 3306,
        "password": "file",           # 用临时 defaults 文件
    },
    "postgres": {
        "label": "PostgreSQL",
        "exe": ("psql",),
        "server": ("postgres",),
        "version_args": ("--version",),
        "default_port": 5432,
        "password": "env",            # PGPASSWORD
    },
    "redis": {
        "label": "Redis",
        "exe": ("redis-cli",),
        "server": ("redis-server",),
        "version_args": ("--version",),
        "default_port": 6379,
        "password": "env",            # REDISCLI_AUTH
    },
    # MongoDB 的凭据只能进 argv / 连接串，会被 ps 看见 —— 明确不支持，不留暗门
    "mongodb": {
        "label": "MongoDB",
        "exe": ("mongosh", "mongo"),
        "server": ("mongod",),
        "version_args": ("--version",),
        "default_port": 27017,
        "password": "unsupported",
    },
}

SUPPORTED = ("mysql", "postgres", "redis")

# 安装目录名线索：数据库常被装在盘根（如 H:\pgsql18、D:\mysql、/www/server/mysql），
# 不在 PATH 里。**只按 PATH 找会把"装了但没配 PATH"判成未安装**（真踩过：
# 本机 PG 18 在 H:\pgsql18，db.scan 报"未安装"）。
_DIR_HINTS = {
    "mysql": ("mysql", "mariadb"),
    "postgres": ("postgres", "pgsql", "postgresql"),
    "redis": ("redis",),
    "mongodb": ("mongodb", "mongo"),
}

# 额外目录由调用方注入（扩展读配置后传进来），这里只做机制层缓存
_EXTRA_DIRS: list = []


def set_extra_dirs(dirs) -> None:
    """注入额外客户端目录（扩展从配置读），下次查找生效。"""
    global _EXTRA_DIRS
    _EXTRA_DIRS = [str(d) for d in (dirs or []) if str(d).strip()]


def _disk_roots() -> list:
    """列出可能的盘根 / 顶层目录（Windows 逐盘，POSIX 只看 / 与常见前缀）。"""
    if os.name != 'nt':
        return ['/usr', '/usr/local', '/opt', '/www/server', '/snap']
    roots = []
    for letter in 'CDEFGHIJK':
        d = f'{letter}:\\'
        if os.path.isdir(d):
            roots.append(d)
    return roots


def discover_dirs(kind: str, ttl: float = 600.0) -> list:
    """扫盘根下的常见安装目录，返回其中的 bin 目录。

    每个盘根只列一层（不递归），命中名字线索后再看它自己与它的 bin 子目录 ——
    足够发现 `H:\\pgsql18`、`C:\\Program Files\\PostgreSQL\\18`、`/www/server/mysql`
    这类布局，又不会把磁盘扫穿。

    找不到就返回空列表（不报错）：没装数据库的机器上这一步必须是安静的。
    """
    hints = _DIR_HINTS.get(kind, ())
    if not hints:
        return []
    out = []

    def _push(base):
        if not base or not os.path.isdir(base):
            return
        for cand in (base, os.path.join(base, 'bin')):
            if os.path.isdir(cand) and cand not in out:
                out.append(cand)

    for root in _disk_roots():
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            low = name.lower()
            if any(h in low for h in hints):
                _push(os.path.join(root, name))
                if 'program files' in low or 'server' in low:   # 再深一层（PG\18）
                    try:
                        for sub in os.listdir(os.path.join(root, name)):
                            _push(os.path.join(root, name, sub))
                    except OSError:
                        pass
    for d in _EXTRA_DIRS:
        _push(d)
    return out


def which(kind: str) -> str:
    """返回该类型客户端的可执行路径，未安装返回 ''。

    查找顺序：PATH → 盘根常见安装目录。第二条是为了「装了但没配 PATH」的场景 ——
    运维面板必须能管到这种机器。
    """
    d = DRIVERS.get(kind)
    if not d:
        return ""
    for name in d["exe"]:
        p = shutil.which(name)
        if p:
            return p
    for base in discover_dirs(kind):
        for name in d["exe"]:
            for ext in ('.exe', '.cmd', '.bat', ''):
                cand = os.path.join(base, name + ext)
                if os.path.isfile(cand):
                    return cand
    return ""


def which_server(kind: str) -> str:
    """找服务端可执行（mysqld / postgres / redis-server 等）。"""
    d = DRIVERS.get(kind)
    if not d:
        return ""
    for name in d["server"]:
        p = shutil.which(name)
        if p:
            return p
    for base in discover_dirs(kind):
        for name in d["server"]:
            for ext in ('.exe', ''):
                cand = os.path.join(base, name + ext)
                if os.path.isfile(cand):
                    return cand
    return ""


def scan(*kinds) -> dict:
    """探测客户端与服务端是否可用，并取版本。拿不到版本就给空串，不编造。"""
    pr = _procs()
    out = {}
    for k in (kinds or tuple(DRIVERS)):
        d = DRIVERS.get(k)
        if not d:
            continue
        client = which(k)
        server = which_server(k)
        dirs = discover_dirs(k)
        ver = ""
        if client:
            r = pr.run([client, *d["version_args"]], timeout=8)
            ver = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[0][:80] if (r.stdout or r.stderr) else ""
        out[k] = {
            "kind": k,
            "label": d["label"],
            "client": client,
            "client_available": bool(client),
            "server": server,
            "server_available": bool(server),
            "version": ver,
            "default_port": d["default_port"],
            "password_mode": d["password"],
            "supported": k in SUPPORTED,
            "searched_dirs": dirs,          # 面板上直接告诉用户"我找了哪些地方"
        }
    return out


# ── 凭据文件（MySQL）────────────────────────────────────────
def _write_defaults(user: str, password: str, extra: dict | None = None) -> str:
    """写临时 MySQL 选项文件，返回目录路径（调用方负责清理）。"""
    d = tempfile.mkdtemp(prefix="zp-db-")
    path = os.path.join(d, "client.cnf")

    def esc(v: str) -> str:
        # MySQL 选项文件里 " 与 \ 需要转义；换行/回车在无引号场景下会破坏结构
        return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")

    lines = ["[client]"]
    if user:
        lines.append(f'user="{esc(user)}"')
    if password:
        lines.append(f'password="{esc(password)}"')
    for k, v in (extra or {}).items():
        lines.append(f'{k}="{esc(v)}"')
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return d


def _cleanup(d: str):
    if d:
        shutil.rmtree(d, ignore_errors=True)


# ── 执行 ────────────────────────────────────────────────────
def _conn_of(conn: dict) -> dict:
    c = dict(conn or {})
    kind = str(c.get("kind") or "").lower()
    if kind not in DRIVERS:
        raise ValueError(f"不支持的数据库类型: {kind or '(空)'}")
    if kind == "mongodb":
        raise ValueError("MongoDB 的口令只能进入命令行/连接串（会被 ps 看到），暂不支持")
    c["kind"] = kind
    c["host"] = str(c.get("host") or "127.0.0.1")
    c["port"] = int(c.get("port") or DRIVERS[kind]["default_port"])
    c["user"] = str(c.get("user") or "")
    c["password"] = str(c.get("password") or "")
    c["database"] = str(c.get("database") or "")
    return c


def _parse_tsv(text: str) -> dict:
    """把 TSV（首行列名）解析成 {columns, rows}。空输出返回空结构。"""
    lines = [l for l in (text or "").splitlines()]
    if not lines:
        return {"columns": [], "rows": []}
    columns = [c.strip() for c in lines[0].split("\t")]
    rows = []
    for l in lines[1:]:
        if not l.strip():
            continue
        cells = l.split("\t")
        if len(cells) < len(columns):
            cells += [""] * (len(columns) - len(cells))
        rows.append(dict(zip(columns, cells[:len(columns)])))
    return {"columns": columns, "rows": rows}


def run_sql(conn: dict, sql: str, timeout: float = 15.0, max_output: int = 200_000) -> dict:
    """执行一条 SQL，返回结果集（MySQL / PostgreSQL）。

    返回 `{ok, columns, rows, raw, duration_ms, error}`。
    失败时 `ok=False` 且 `error` 是客户端的原始报错（原样透出，便于排障）。
    """
    pr = _procs()
    c = _conn_of(conn)
    kind = c["kind"]
    if kind == "redis":
        return {"ok": False, "columns": [], "rows": [], "raw": "",
                "error": "Redis 不是 SQL 数据库，请用 run_argv()"}
    exe = which(kind)
    if not exe:
        return {"ok": False, "columns": [], "rows": [], "raw": "", "error":
                f"未找到 {DRIVERS[kind]['label']} 客户端（{('/'.join(DRIVERS[kind]['exe']))}）"}

    tmpdir = ""
    env = None
    argv = [exe]
    try:
        if kind == "mysql":
            tmpdir = _write_defaults(c["user"], c["password"])
            argv += [f"--defaults-extra-file={os.path.join(tmpdir, 'client.cnf')}",
                     "-h", c["host"], "-P", str(c["port"]), "--batch"]
            if c["database"]:
                argv += ["-D", c["database"]]
            argv += ["-e", sql]
        else:  # postgres
            env = dict(os.environ)
            if c["password"]:
                env["PGPASSWORD"] = c["password"]
            argv += ["-h", c["host"], "-p", str(c["port"]), "-U", c["user"],
                     "-w",                       # 没口令时不交互提示，避免卡死
                     "-A", "-F", "\t",           # 非对齐 + TSV
                     "--pset", "footer=off"]
            if c["database"]:
                argv += ["-d", c["database"]]
            argv += ["-c", sql]

        r = pr.run(argv, timeout=timeout,
                   env=env if env else None, shell=False,
                   max_output=max_output)
        raw = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        ok = (r.returncode == 0) and not r.timed_out
        if r.timed_out:
            return {"ok": False, "columns": [], "rows": [], "raw": raw,
                    "duration_ms": round(r.duration * 1000),
                    "error": f"执行超时（{timeout}s）"}
        parsed = _parse_tsv(raw) if ok else {"columns": [], "rows": []}
        return {
            "ok": ok,
            "columns": parsed["columns"],
            "rows": parsed["rows"],
            "raw": raw,
            "duration_ms": round(r.duration * 1000),
            "error": "" if ok else (err or raw or f"退出码 {r.returncode}"),
        }
    except Exception as e:
        return {"ok": False, "columns": [], "rows": [], "raw": "", "error":
                f"{type(e).__name__}: {e}"}
    finally:
        _cleanup(tmpdir)


def run_argv(conn: dict, tail: list, timeout: float = 10.0) -> dict:
    """给 Redis 这类「命令即参数」的客户端用（如 `INFO`、`DBSIZE`）。"""
    pr = _procs()
    c = _conn_of(conn)
    if c["kind"] != "redis":
        return {"ok": False, "raw": "", "error": "run_argv 仅供 Redis 使用"}
    exe = which("redis")
    if not exe:
        return {"ok": False, "raw": "", "error": "未找到 redis-cli"}
    env = dict(os.environ)
    if c["password"]:
        env["REDISCLI_AUTH"] = c["password"]
    argv = [exe, "-h", c["host"], "-p", str(c["port"]), "--no-auth-warning",
            *(str(x) for x in tail)]
    try:
        r = pr.run(argv, timeout=timeout, env=env, shell=False)
        raw = (r.stdout or "").strip()
        return {"ok": r.returncode == 0 and not r.timed_out,
                "raw": raw,
                "duration_ms": round(r.duration * 1000),
                "error": "" if r.returncode == 0 else ((r.stderr or "").strip() or raw)}
    except Exception as e:
        return {"ok": False, "raw": "", "error": f"{type(e).__name__}: {e}"}


def test(conn: dict, timeout: float = 8.0) -> dict:
    """连通性测试。返回 {ok, latency_ms, version, error}。"""
    t0 = time.perf_counter()
    c = _conn_of(conn)
    try:
        if c["kind"] == "redis":
            r = run_argv(c, ["PING"], timeout)
            ok = r["ok"] and "PONG" in (r["raw"] or "").upper()
            return {"ok": ok, "latency_ms": round((time.perf_counter() - t0) * 1000),
                    "version": "", "error": "" if ok else (r["error"] or r["raw"])}
        sql = "SELECT VERSION();" if c["kind"] == "mysql" else "SELECT version();"
        r = run_sql(c, sql, timeout=timeout)
        ver = ""
        if r["ok"] and r["rows"]:
            ver = "|".join(str(v) for v in r["rows"][0].values())
        return {"ok": r["ok"], "latency_ms": round((time.perf_counter() - t0) * 1000),
                "version": ver[:80], "error": r["error"]}
    except Exception as e:
        return {"ok": False, "latency_ms": round((time.perf_counter() - t0) * 1000),
                "version": "", "error": f"{type(e).__name__}: {e}"}


# ══════════════════════════════════════════════════════════
# 管理动作（DDL / DCL / 导出导入）
# ══════════════════════════════════════════════════════════
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")

# 允许授予的权限白名单（不做字符串拼接的自由发挥）
GRANTABLE = {
    "postgres": ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES",
                 "TRIGGER", "CREATE", "CONNECT", "TEMPORARY", "USAGE", "ALL"),
    "mysql": ("SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER",
              "INDEX", "REFERENCES", "CREATE VIEW", "SHOW VIEW", "TRIGGER", "EXECUTE",
              "CREATE ROUTINE", "EVENT", "ALL PRIVILEGES"),
}


def check_ident(name: str, what: str = "标识符") -> str:
    """严格校验库名/用户名，不合法直接抛错。

    为什么必须校验：这些名字要拼进 SQL，客户端又不支持参数化 DDL ——
    白名单字符集 + 随后加引号，是能同时防注入又保持可用的做法。
    """
    s = str(name or "").strip()
    if not IDENT_RE.match(s):
        raise ValueError(f"{what}不合法：{name!r}（只允许字母数字下划线 $，且不以数字开头，≤63 字符）")
    return s


def quote_ident(kind: str, name: str) -> str:
    """按方言加引号（PG 双引号 / MySQL 反引号）。"""
    s = check_ident(name)
    return f'"{s}"' if kind == "postgres" else f"`{s}`"


def quote_literal(s: str) -> str:
    """SQL 字符串字面量（单引号双写；不走 shell，无需再转义反斜杠）。"""
    return "'" + str(s).replace("'", "''") + "'"


def run_admin(conn: dict, sql: str, timeout: float = 30.0) -> dict:
    """执行管理语句（CREATE / DROP / GRANT / ALTER …）。

    与 run_sql 共用同一套凭据传递方式，但**不做只读校验** ——
    鉴权与"是否有权做这件事"属于调用方（扩展）的判断，机制层只负责安全地执行。
    """
    r = run_sql(conn, sql, timeout=timeout)
    return {'ok': r['ok'], 'raw': r.get('raw', ''), 'error': r.get('error', ''),
            'duration_ms': r.get('duration_ms', 0), 'sql': sql}


def which_tool(kind: str, tool: str) -> str:
    """找同系列的配套工具（pg_dump / pg_restore / mysqldump / mysqladmin…）。

    先看 PATH，再去客户端所在目录与安装目录找 —— 这些工具几乎总是和客户端同目录，
    而该目录可能压根不在 PATH 里（H:\\pgsql18\\bin 就是）。
    """
    names = [f"{tool}.exe", f"{tool}.cmd", tool] if os.name == 'nt' else [tool]
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    bases = []
    cli = which(kind)
    if cli:
        bases.append(os.path.dirname(cli))
    bases += discover_dirs(kind)
    for base in bases:
        for n in names:
            cand = os.path.join(base, n)
            if os.path.isfile(cand):
                return cand
    return ""


def dump(conn: dict, out_path: str, database: str = "", timeout: float = 300.0) -> dict:
    """导出为 SQL 文件（pg_dump / mysqldump），返回 {ok, path, size, error}。"""
    pr = _procs()
    c = _conn_of(conn)
    kind = c['kind']
    if kind not in ('postgres', 'mysql'):
        return {'ok': False, 'error': f'{kind} 暂不支持导出'}
    db = str(database or c.get('database') or '').strip()
    if not db:
        return {'ok': False, 'error': '未指定要导出的库'}
    check_ident(db, '库名')
    out_path = str(out_path or '').strip()
    if not out_path:
        return {'ok': False, 'error': '未指定导出路径'}
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    tmpdir, env, argv = '', None, []
    try:
        if kind == 'postgres':
            exe = which_tool('postgres', 'pg_dump')
            if not exe:
                return {'ok': False, 'error': '未找到 pg_dump（与 psql 同目录）'}
            env = dict(os.environ)
            if c['password']:
                env['PGPASSWORD'] = c['password']
            argv = [exe, '-h', c['host'], '-p', str(c['port']), '-U', c['user'],
                    '-w', '-f', out_path, '-d', db]
        else:
            exe = which_tool('mysql', 'mysqldump')
            if not exe:
                return {'ok': False, 'error': '未找到 mysqldump（与 mysql 同目录）'}
            tmpdir = _write_defaults(c['user'], c['password'])
            argv = [exe, f"--defaults-extra-file={os.path.join(tmpdir, 'client.cnf')}",
                    '-h', c['host'], '-P', str(c['port']), '--result-file=' + out_path, db]
        r = pr.run(argv, timeout=timeout, env=env, shell=False, max_output=64 * 1024)
        ok = r.returncode == 0 and not r.timed_out
        size = os.path.getsize(out_path) if os.path.isfile(out_path) else 0
        return {'ok': ok and size > 0, 'path': out_path, 'size': size,
                'database': db,
                'error': '' if ok else ((r.stderr or '').strip() or f'退出码 {r.returncode}')}
    except Exception as e:
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}
    finally:
        _cleanup(tmpdir)


def restore(conn: dict, path: str, database: str = '', timeout: float = 600.0) -> dict:
    """从 SQL 文件导入（psql -f / mysql < file）。"""
    pr = _procs()
    c = _conn_of(conn)
    kind = c['kind']
    if kind not in ('postgres', 'mysql'):
        return {'ok': False, 'error': f'{kind} 暂不支持导入'}
    path = str(path or '').strip()
    if not os.path.isfile(path):
        return {'ok': False, 'error': f'文件不存在: {path}'}
    db = str(database or c.get('database') or '').strip()
    tmpdir, env, argv = '', None, []
    try:
        if kind == 'postgres':
            exe = which_tool('postgres', 'psql')
            if not exe:
                return {'ok': False, 'error': '未找到 psql'}
            env = dict(os.environ)
            if c['password']:
                env['PGPASSWORD'] = c['password']
            argv = [exe, '-h', c['host'], '-p', str(c['port']), '-U', c['user'], '-w',
                    '-v', 'ON_ERROR_STOP=1', '-f', path]
            if db:
                argv += ['-d', db]
        else:
            exe = which_tool('mysql', 'mysql')
            if not exe:
                return {'ok': False, 'error': '未找到 mysql 客户端'}
            tmpdir = _write_defaults(c['user'], c['password'])
            argv = [exe, f"--defaults-extra-file={os.path.join(tmpdir, 'client.cnf')}",
                    '-h', c['host'], '-P', str(c['port']), db] if db else \
                   [exe, f"--defaults-extra-file={os.path.join(tmpdir, 'client.cnf')}",
                    '-h', c['host'], '-P', str(c['port'])]
        # 导入喂 stdin（文件句柄直传）—— 不再拼 shell 字符串（路径含引号即可注入）
        with open(path, 'rb') as fh:
            r = pr.run(argv, timeout=timeout, env=env, shell=False,
                       max_output=64 * 1024, stdin=fh)
        ok = r.returncode == 0 and not r.timed_out
        return {'ok': ok, 'path': path, 'database': db,
                'error': '' if ok else ((r.stderr or '').strip() or f'退出码 {r.returncode}')}
    except Exception as e:
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}
    finally:
        _cleanup(tmpdir)
