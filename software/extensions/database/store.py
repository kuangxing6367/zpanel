# -*- coding: utf-8 -*-
"""
数据库连接的持久化与操作（database 扩展的存储层）

三件被刻意分开的事：

- **驱动**（怎么连、凭据怎么传）→ `dbclient` 机制包
- **加密**（口令怎么存）→ `secretbox` 机制包
- **启停**（服务怎么拉起）→ `svcmgr` 机制包

这里只负责：建表、CRUD、按数据库类型挑合适的 SQL、以及把结果整理成前端能直接
渲染的形状。**口令永不出库**：对外只给 `has_password`。
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid

logger = logging.getLogger('zernus')

# 各类型的「库清单 / 表清单 / 状态 / 活动连接」语句。
# 拿不到就留空字符串，调用方返回空结果并说明原因 —— 不用通用 SQL 硬套。
SQL = {
    "mysql": {
        "databases": "SELECT schema_name AS name, "
                     "ROUND(SUM(data_length + index_length) / 1024 / 1024, 1) AS size_mb, "
                     "COUNT(*) AS tables "
                     "FROM information_schema.schemata "
                     "LEFT JOIN information_schema.tables "
                     "ON tables.table_schema = schemata.schema_name "
                     "GROUP BY schema_name;",
        "tables": "SELECT table_name AS name, table_rows AS rows, "
                  "ROUND((data_length + index_length) / 1024 / 1024, 1) AS size_mb, "
                  "engine FROM information_schema.tables WHERE table_schema = DATABASE();",
        "status": "SHOW GLOBAL STATUS;",
        "processlist": "SHOW PROCESSLIST;",
    },
    "postgres": {
        "databases": "SELECT datname AS name, "
                     "ROUND(pg_database_size(datname) / 1024.0 / 1024.0, 1) AS size_mb "
                     "FROM pg_database WHERE datistemplate = false;",
        "tables": "SELECT tablename AS name FROM pg_tables WHERE schemaname = 'public';",
        # 状态必须给「指标名 + 值」两列：单列 count(*) 会让上层解析出空指标
        # （真踩过：db.status 对 PG 返回 {}）。
        "status": (
            "SELECT 'connections' AS metric, count(*)::text AS value FROM pg_stat_activity "
            "UNION ALL SELECT 'active', count(*)::text FROM pg_stat_activity WHERE state = 'active' "
            "UNION ALL SELECT 'idle', count(*)::text FROM pg_stat_activity WHERE state = 'idle' "
            "UNION ALL SELECT 'databases', count(*)::text FROM pg_database WHERE datistemplate = false "
            "UNION ALL SELECT 'max_connections', current_setting('max_connections') "
            "UNION ALL SELECT 'version', current_setting('server_version') "
            "UNION ALL SELECT 'uptime_s', "
            "extract(epoch FROM now() - pg_postmaster_start_time())::bigint::text "
            "UNION ALL SELECT 'current_db_size_mb', "
            "ROUND(pg_database_size(current_database()) / 1024.0 / 1024.0, 1)::text"
        ),
        "processlist": "SELECT pid, usename AS user, client_addr AS host, state, "
                       "left(query, 80) AS query FROM pg_stat_activity;",
    },
}

# SHOW GLOBAL STATUS 里值得单独拎出来的项
MYSQL_STATUS_KEYS = ("Uptime", "Threads_connected", "Threads_running", "Questions",
                     "Slow_queries", "Bytes_received", "Bytes_sent", "Max_used_connections")


def _project_root() -> str:
    """项目根目录（software/extensions/database/store.py 往上 4 层）。"""
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))


class ConnectionStore:
    """数据库连接清单 + 操作。"""

    def __init__(self, fw, dbclient, secretbox, svcmgr=None, log=None,
                 read_only: bool = True, max_rows: int = 500, compat=None):
        self.fw = fw
        self.dbc = dbclient
        self.sb = secretbox
        self.svc = svcmgr
        self.compat = compat          # 版本兼容层：按服务端版本给差异与注意事项
        self._log = log or (lambda m: logger.info(m))
        self.read_only = bool(read_only)
        self.max_rows = int(max_rows)
        self._key = None
        self.ensure_table()

    # ── 主密钥 ──────────────────────────────────────────────
    @property
    def key(self) -> bytes:
        if self._key is None:
            path = os.path.join(_project_root(), 'data', 'secretbox.key')
            self._key = self.sb.load_or_create_key(path)
        return self._key

    def key_fingerprint(self) -> str:
        return self.sb.fingerprint(self.key)

    # ── 持久化 ──────────────────────────────────────────────
    def ensure_table(self):
        self.fw.db.execute("""
            CREATE TABLE IF NOT EXISTS db_connections (
                id           TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                kind         TEXT NOT NULL,
                host         TEXT DEFAULT '',
                port         INTEGER DEFAULT 0,
                user         TEXT DEFAULT '',
                password_enc TEXT DEFAULT '',
                database     TEXT DEFAULT '',
                enabled      INTEGER DEFAULT 1,
                tags         TEXT DEFAULT '',
                remark       TEXT DEFAULT '',
                last_ok_at   REAL DEFAULT 0,
                last_error   TEXT DEFAULT '',
                created_at   REAL,
                updated_at   REAL
            )
        """)

    def _row_to_conn(self, r: dict, with_password: bool = False) -> dict:
        out = {
            'id': r['id'], 'name': r['name'], 'kind': r['kind'],
            'host': r['host'], 'port': r['port'] or self.dbc.DRIVERS.get(
                r['kind'], {}).get('default_port', 0),
            'user': r['user'], 'database': r['database'],
            'enabled': bool(r['enabled']),
            'tags': [t for t in (r['tags'] or '').split(',') if t],
            'remark': r['remark'] or '',
            'has_password': bool(r['password_enc']),
            'last_ok_at': r['last_ok_at'] or 0,
            'last_error': r['last_error'] or '',
            'created_at': r['created_at'] or 0,
            'updated_at': r['updated_at'] or 0,
        }
        if with_password:
            out['password'] = ''
            try:
                if r['password_enc']:
                    out['password'] = self.sb.open_sealed(r['password_enc'], self.key)
            except Exception as e:
                logger.warning("[database] 解开口令失败（主密钥变了？）: %s", e)
                out['password'] = ''
                out['error'] = '口令无法解开：主密钥不匹配或密文损坏'
        return out

    def list(self) -> dict:
        rows = self.fw.db.query("SELECT * FROM db_connections ORDER BY created_at") or []
        return {'count': len(rows),
                'connections': [self._row_to_conn(r) for r in rows],
                'read_only': self.read_only,
                'key_fingerprint': self.key_fingerprint()}

    def get(self, cid: str, with_password: bool = False):
        rows = self.fw.db.query("SELECT * FROM db_connections WHERE id=?", (cid,)) or []
        if not rows:
            return None
        return self._row_to_conn(rows[0], with_password)

    def create(self, data: dict) -> dict:
        name = str(data.get('name') or '').strip()
        kind = str(data.get('kind') or '').strip().lower()
        if not name:
            return {'ok': False, 'error': '名称不能为空'}
        if kind not in self.dbc.DRIVERS:
            return {'ok': False, 'error': f'不支持的数据库类型: {kind or "(空)"}'}
        if kind == 'mongodb':
            return {'ok': False, 'error': 'MongoDB 口令只能进命令行（会被 ps 看到），暂不支持'}
        cid = uuid.uuid4().hex[:16]
        now = time.time()
        pwd = str(data.get('password') or '')
        self.fw.db.execute(
            "INSERT INTO db_connections "
            "(id, name, kind, host, port, user, password_enc, database, enabled, "
            " tags, remark, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, name, kind,
             str(data.get('host') or '127.0.0.1'),
             int(data.get('port') or self.dbc.DRIVERS[kind]['default_port']),
             str(data.get('user') or ''),
             self.sb.seal(pwd, self.key),
             str(data.get('database') or ''),
             1 if data.get('enabled', True) else 0,
             ','.join(str(t) for t in (data.get('tags') or []) if t),
             str(data.get('remark') or ''), now, now))
        return {'ok': True, 'conn': self.get(cid)}

    def update(self, cid: str, data: dict) -> dict:
        if not self.get(cid):
            return {'ok': False, 'error': '连接不存在'}
        sets, vals = [], []
        for col in ('name', 'kind', 'host', 'user', 'database', 'remark'):
            if data.get(col) is not None:
                sets.append(f"{col}=?"); vals.append(str(data[col]))
        if data.get('port') is not None:
            sets.append("port=?"); vals.append(int(data['port']))
        if data.get('enabled') is not None:
            sets.append("enabled=?"); vals.append(1 if data['enabled'] else 0)
        if data.get('tags') is not None:
            sets.append("tags=?"); vals.append(','.join(str(t) for t in data['tags'] if t))
        if data.get('password') is not None:
            # 传空串表示「清除口令」，与「不修改」区分开
            sets.append("password_enc=?")
            vals.append(self.sb.seal(str(data['password']), self.key))
        if not sets:
            return {'ok': True, 'conn': self.get(cid)}
        sets.append("updated_at=?"); vals.append(time.time())
        vals.append(cid)
        self.fw.db.execute(f"UPDATE db_connections SET {','.join(sets)} WHERE id=?", tuple(vals))
        return {'ok': True, 'conn': self.get(cid)}

    def remove(self, cid: str) -> dict:
        if not self.get(cid):
            return {'ok': False, 'error': '连接不存在'}
        self.fw.db.execute("DELETE FROM db_connections WHERE id=?", (cid,))
        return {'ok': True}

    def _touch(self, cid: str, ok: bool, err: str = ''):
        try:
            if ok:
                self.fw.db.execute(
                    "UPDATE db_connections SET last_ok_at=?, last_error='' WHERE id=?",
                    (time.time(), cid))
            else:
                self.fw.db.execute(
                    "UPDATE db_connections SET last_error=? WHERE id=?",
                    (str(err)[:300], cid))
        except Exception:
            pass

    # ── 探测 ────────────────────────────────────────────────
    def scan(self) -> dict:
        return {'drivers': self.dbc.scan(), 'supported': list(self.dbc.SUPPORTED),
                'read_only': self.read_only}

    # ── 操作 ────────────────────────────────────────────────
    def test(self, cid: str) -> dict:
        c = self.get(cid, with_password=True)
        if c is None:
            return {'ok': False, 'error': '连接不存在'}
        r = self.dbc.test(c)
        self._touch(cid, r['ok'], r.get('error', ''))
        r['name'] = c['name']
        return r

    def databases(self, cid: str) -> dict:
        c = self.get(cid, with_password=True)
        if c is None:
            return {'ok': False, 'error': '连接不存在'}
        if c['kind'] == 'redis':
            r = self.dbc.run_argv(c, ['INFO', 'keyspace'])
            return {'ok': r['ok'], 'kind': 'redis', 'rows': [],
                    'raw': r['raw'], 'error': r['error']}
        sql = SQL.get(c['kind'], {}).get('databases')
        if not sql:
            return {'ok': False, 'error': f'{c["kind"]} 暂无库清单语句'}
        r = self.dbc.run_sql(c, sql)
        self._touch(cid, r['ok'], r.get('error', ''))
        rows = r['rows'][:self.max_rows]
        return {'ok': r['ok'], 'kind': c['kind'], 'columns': r['columns'],
                'rows': rows, 'count': len(rows),
                'duration_ms': r.get('duration_ms'), 'error': r['error']}

    def tables(self, cid: str, database: str = '') -> dict:
        c = self.get(cid, with_password=True)
        if c is None:
            return {'ok': False, 'error': '连接不存在'}
        if database:
            c['database'] = database
        if c['kind'] == 'redis':
            r = self.dbc.run_argv(c, ['DBSIZE'])
            return {'ok': r['ok'], 'kind': 'redis', 'rows': [],
                    'raw': r['raw'], 'error': r['error']}
        if not c['database']:
            return {'ok': False, 'error': '未指定库名（连接里没填，也没在参数里给）'}
        sql = SQL.get(c['kind'], {}).get('tables')
        if not sql:
            return {'ok': False, 'error': f'{c["kind"]} 暂无表清单语句'}
        r = self.dbc.run_sql(c, sql)
        self._touch(cid, r['ok'], r.get('error', ''))
        rows = r['rows'][:self.max_rows]
        return {'ok': r['ok'], 'kind': c['kind'], 'database': c['database'],
                'columns': r['columns'], 'rows': rows, 'count': len(rows),
                'duration_ms': r.get('duration_ms'), 'error': r['error']}

    def server_version(self, cid: str) -> dict:
        """取**服务端**版本串（不是客户端）。版本兼容归层的输入。"""
        c = self.get(cid, with_password=True)
        if c is None:
            return {'ok': False, 'error': '连接不存在'}
        kind = c['kind']
        try:
            if kind == 'redis':
                r = self.dbc.run_argv(c, ['INFO', 'server'])
                for line in (r.get('raw') or '').splitlines():
                    if line.lower().startswith('redis_version:'):
                        return {'ok': True, 'version': line.split(':', 1)[1].strip()}
                return {'ok': False, 'error': '未取到 redis_version'}
            if kind == 'postgres':
                sql = "SELECT current_setting('server_version') AS v;"
            elif kind in ('mysql', 'mariadb'):
                sql = 'SELECT VERSION() AS v;'
            else:
                return {'ok': False, 'error': f'{kind} 暂不支持版本探测'}
            r = self.dbc.run_sql(c, sql)
            if not r['ok']:
                return {'ok': False, 'error': r.get('error')}
            rows = r.get('rows') or []
            v = str(list(rows[0].values())[0]).strip() if rows else ''
            return {'ok': True, 'version': v} if v else {'ok': False, 'error': '版本串为空'}
        except Exception as e:
            return {'ok': False, 'error': f'{type(e).__name__}: {e}'}

    def compat_profile(self, cid: str) -> dict:
        """该连接服务端的**版本兼容层档案**。

        面板据此直接告诉运维「这台 8.4 的 mysql_native_password 默认已关」
        「这台 PG 15+ 的 public 模式不再给普通用户 CREATE」——
        版本差异统一由 compat 机制包给出，本扩展里**不写 `if version >= ...`**。
        """
        if self.compat is None:
            return {'ok': False, 'error': 'compat 机制包未加载'}
        c = self.get(cid)
        if c is None:
            return {'ok': False, 'error': '连接不存在'}
        v = self.server_version(cid)
        if not v.get('ok'):
            return {'ok': False, 'error': v.get('error')}
        ver = v['version']
        kind = c['kind']
        # MariaDB 的版本串自带 "MariaDB"，按它归族；否则按连接类型
        fam = 'mariadb' if (kind == 'mysql' and 'mariadb' in ver.lower()) else kind
        try:
            exp = self.compat.explain(fam, ver)
            prof = self.compat.db_profile(fam, ver)
        except Exception as e:
            return {'ok': False, 'error': f'{type(e).__name__}: {e}'}
        return {
            'ok': True, 'family': fam, 'server_version': ver,
            'layer': exp.get('layer', ''), 'title': exp.get('title', ''),
            'traits': exp.get('traits', []), 'gained': exp.get('gained', []),
            'dropped': exp.get('dropped', []), 'compared_to': exp.get('compared_to', ''),
            'profile': prof,
        }

    def db_status(self, cid: str) -> dict:
        c = self.get(cid, with_password=True)
        if c is None:
            return {'ok': False, 'error': '连接不存在'}
        if c['kind'] == 'redis':
            r = self.dbc.run_argv(c, ['INFO'])
            info = {}
            for line in (r.get('raw') or '').splitlines():
                if ':' in line and not line.startswith('#'):
                    k, _, v = line.partition(':')
                    if k.strip() in ('redis_version', 'connected_clients', 'used_memory_human',
                                     'uptime_in_seconds', 'total_commands_processed',
                                     'keyspace_hits', 'keyspace_misses'):
                        info[k.strip()] = v.strip()
            return {'ok': r['ok'], 'kind': 'redis', 'metrics': info, 'error': r['error']}
        sql = SQL.get(c['kind'], {}).get('status')
        if not sql:
            return {'ok': False, 'error': f'{c["kind"]} 暂无状态语句'}
        r = self.dbc.run_sql(c, sql)
        self._touch(cid, r['ok'], r.get('error', ''))
        metrics = {}
        for row in r['rows']:
            vals = list(row.values())
            if len(vals) >= 2:
                metrics[str(vals[0])] = vals[1]
        if c['kind'] == 'mysql':
            metrics = {k: metrics.get(k) for k in MYSQL_STATUS_KEYS if k in metrics}
        return {'ok': r['ok'], 'kind': c['kind'], 'metrics': metrics,
                'duration_ms': r.get('duration_ms'), 'error': r['error']}

    def processlist(self, cid: str) -> dict:
        c = self.get(cid, with_password=True)
        if c is None:
            return {'ok': False, 'error': '连接不存在'}
        if c['kind'] == 'redis':
            r = self.dbc.run_argv(c, ['CLIENT', 'LIST'])
            return {'ok': r['ok'], 'kind': 'redis', 'rows': [], 'raw': r['raw'],
                    'error': r['error']}
        sql = SQL.get(c['kind'], {}).get('processlist')
        if not sql:
            return {'ok': False, 'error': f'{c["kind"]} 暂无活动连接语句'}
        r = self.dbc.run_sql(c, sql)
        self._touch(cid, r['ok'], r.get('error', ''))
        rows = r['rows'][:self.max_rows]
        return {'ok': r['ok'], 'kind': c['kind'], 'columns': r['columns'],
                'rows': rows, 'count': len(rows), 'error': r['error']}

    def query(self, cid: str, sql: str) -> dict:
        from software.extensions.database.main import _readonly
        c = self.get(cid, with_password=True)
        if c is None:
            return {'ok': False, 'error': '连接不存在'}
        if c['kind'] == 'redis':
            return {'ok': False, 'error': 'Redis 不是 SQL 数据库（请用状态/活动连接查看）'}
        if self.read_only:
            ok, why = _readonly(sql)
            if not ok:
                return {'ok': False, 'error': f'只读模式：{why}'}
        r = self.dbc.run_sql(c, sql)
        self._touch(cid, r['ok'], r.get('error', ''))
        rows = r['rows'][:self.max_rows]
        return {'ok': r['ok'], 'columns': r['columns'], 'rows': rows,
                'count': len(rows), 'truncated': len(r['rows']) > self.max_rows,
                'duration_ms': r.get('duration_ms'), 'error': r['error']}

    # ── 服务启停（交给系统服务管理器）────────────────────────
    def services(self, kind: str = '') -> dict:
        if self.svc is None:
            return {'ok': False, 'error': 'svcmgr 机制包未加载', 'services': []}
        from software.extensions.database.main import SERVICE_CANDIDATES
        kinds = [kind] if kind else list(SERVICE_CANDIDATES)
        out = []
        for k in kinds:
            cands = SERVICE_CANDIDATES.get(k) or ()
            st = self.svc.resolve(cands)
            out.append({'kind': k, 'candidates': list(cands), **st})
        return {'ok': True, 'backend': self.svc.backend(),
                'platforms': self.svc.platforms(), 'services': out}

    def service_act(self, kind: str, action: str, name: str = '') -> dict:
        if self.svc is None:
            return {'ok': False, 'error': 'svcmgr 机制包未加载'}
        from software.extensions.database.main import SERVICE_CANDIDATES
        cands = SERVICE_CANDIDATES.get(kind)
        if not cands:
            return {'ok': False, 'error': f'未知数据库类型: {kind or "(空)"}'}
        target = name or ''
        if not target:
            st = self.svc.resolve(cands)
            if not st.get('found'):
                return {'ok': False, 'error': st.get('error') or '未找到对应服务'}
            target = st['name']
        r = self.svc.act(action, target)
        r['kind'] = kind
        return r

    # ── 管理动作（建库 / 建用户 / 授权 / 导出导入）──────────
    # 这些动作**不受只读模式约束**（只读管的是临时查询），但每一步都要：
    # ① 严格校验标识符（白名单字符集 + 方言引号，机制层 check_ident/quote_ident）；
    # ② 危险动作要求显式确认（删库要 confirm 等于库名）。
    def _conn_for_admin(self, cid: str):
        c = self.get(cid, with_password=True)
        if c is None:
            return None, {'ok': False, 'error': '连接不存在'}
        if c['kind'] == 'redis':
            return None, {'ok': False, 'error': 'Redis 不是 SQL 数据库'}
        return c, None

    def create_database(self, cid: str, name: str, owner: str = '',
                        encoding: str = '') -> dict:
        c, err = self._conn_for_admin(cid)
        if err:
            return err
        try:
            ident = self.dbc.quote_ident(c['kind'], name)
            owner = self.dbc.check_ident(owner, '属主') if owner else ''
        except ValueError as e:
            return {'ok': False, 'error': str(e)}
        # PG 没有 CREATE DATABASE IF NOT EXISTS，MySQL 有 —— 统一在面板层先查存在性，
        # 让"已存在"是一个明确的业务结果，而不是客户端报错（幂等：重复点不会炸）。
        exists = self._db_exists(c, name)
        if exists is True:
            return {'ok': False, 'exists': True, 'error': f'库 {name} 已存在'}
        if c['kind'] == 'postgres':
            sql = f'CREATE DATABASE {ident}'
            if owner:
                sql += f' OWNER {self.dbc.quote_ident(c["kind"], owner)}'
            if encoding:
                sql += f' ENCODING {self.dbc.quote_literal(encoding)}'
        else:
            sql = f'CREATE DATABASE {ident}'
            if encoding:
                sql += f' CHARACTER SET {self.dbc.check_ident(encoding, "字符集")}'
        r = self.dbc.run_admin(c, sql, timeout=60)
        return {'ok': r['ok'], 'sql': sql, 'error': r['error']}

    def _db_exists(self, c: dict, name: str):
        """查库是否已存在（PG 连维护库查 catalog；MySQL 用 information_schema）。"""
        try:
            if c['kind'] == 'postgres':
                cc = dict(c, database='postgres')
                sql = ('SELECT 1 FROM pg_database WHERE datname = '
                       + self.dbc.quote_literal(name))
            else:
                sql = ('SELECT 1 FROM information_schema.schemata WHERE schema_name = '
                       + self.dbc.quote_literal(name))
            r = self.dbc.run_sql(cc if c['kind'] == 'postgres' else c, sql, timeout=15)
            return bool(r['ok'] and r['rows'])
        except Exception:
            return None              # 查不了就别拦，交给执行时报错

    def _role_exists(self, c: dict, user: str):
        try:
            if c['kind'] == 'postgres':
                sql = ('SELECT 1 FROM pg_roles WHERE rolname = '
                       + self.dbc.quote_literal(user))
            else:
                sql = ('SELECT 1 FROM mysql.user WHERE User = '
                       + self.dbc.quote_literal(user))
            r = self.dbc.run_sql(c, sql, timeout=15)
            return bool(r['ok'] and r['rows'])
        except Exception:
            return None

    def drop_database(self, cid: str, name: str, confirm: str = '') -> dict:
        c, err = self._conn_for_admin(cid)
        if err:
            return err
        try:
            self.dbc.check_ident(name, '库名')
        except ValueError as e:
            return {'ok': False, 'error': str(e)}
        if str(confirm) != str(name):       # 删库不可逆：要求把库名原样再打一遍
            return {'ok': False, 'error': '删库需要 confirm 填成与库名完全一致'}
        if c['kind'] == 'postgres':
            # PG 不能删当前连接的库 → 连到 postgres 维护库执行
            c = dict(c, database='postgres')
            sql = f'DROP DATABASE {self.dbc.quote_ident(c["kind"], name)}'
        else:
            sql = f'DROP DATABASE {self.dbc.quote_ident(c["kind"], name)}'
        r = self.dbc.run_admin(c, sql, timeout=60)
        return {'ok': r['ok'], 'sql': sql, 'error': r['error']}

    def users(self, cid: str) -> dict:
        c = self.get(cid, with_password=True)
        if c is None:
            return {'ok': False, 'error': '连接不存在'}
        if c['kind'] == 'postgres':
            sql = ("SELECT rolname AS user, rolsuper::text AS superuser, "
                   "rolcreatedb::text AS createdb, rolcanlogin::text AS can_login "
                   "FROM pg_roles WHERE rolname NOT LIKE 'pg\\_%' ORDER BY 1")
        else:
            sql = ("SELECT User AS user, Host AS host, "
                   "IF(Super_priv='Y','true','false') AS superuser "
                   "FROM mysql.user ORDER BY 1,2")
        r = self.dbc.run_sql(c, sql, timeout=20)
        self._touch(cid, r['ok'], r.get('error', ''))
        return {'ok': r['ok'], 'kind': c['kind'], 'columns': r['columns'],
                'rows': r['rows'][:self.max_rows], 'error': r['error']}

    def create_user(self, cid: str, user: str, password: str,
                    superuser: bool = False) -> dict:
        c, err = self._conn_for_admin(cid)
        if err:
            return err
        try:
            ident = self.dbc.quote_ident(c['kind'], user)
        except ValueError as e:
            return {'ok': False, 'error': str(e)}
        if not password:
            return {'ok': False, 'error': '密码不能为空'}
        if self._role_exists(c, user) is True:
            return {'ok': False, 'exists': True, 'error': f'用户 {user} 已存在'}
        lit = self.dbc.quote_literal(password)
        sql = (f'CREATE ROLE {ident} LOGIN PASSWORD {lit}' +
               (' SUPERUSER' if superuser else '')) if c['kind'] == 'postgres' else \
              f"CREATE USER {ident}@'%' IDENTIFIED BY {lit}" + \
              (' ' if not superuser else '')
        r = self.dbc.run_admin(c, sql, timeout=30)
        out = {'ok': r['ok'], 'sql': sql.replace(lit, "'***'"),
               'error': r['error']}
        if r['ok'] and superuser and c['kind'] == 'mysql':
            g = self.dbc.run_admin(c, f'GRANT ALL PRIVILEGES ON *.* TO {ident}'
                                      f"@'%' WITH GRANT OPTION", timeout=30)
            out['grant_sql'] = g['sql']
            out['ok'] = g['ok']
            out['error'] = g['error']
        return out

    def drop_user(self, cid: str, user: str, confirm: str = '') -> dict:
        c, err = self._conn_for_admin(cid)
        if err:
            return err
        try:
            self.dbc.check_ident(user, '用户名')
        except ValueError as e:
            return {'ok': False, 'error': str(e)}
        if str(confirm) != str(user):
            return {'ok': False, 'error': '删用户需要 confirm 填成与用户名完全一致'}
        if self._role_exists(c, user) is False:
            return {'ok': False, 'missing': True, 'error': f'用户 {user} 不存在'}
        u = self.dbc.quote_ident(c['kind'], user)
        if c['kind'] == 'postgres':
            # PG 的角色只要有依赖对象（授权的表、拥有的对象）就删不掉，
            # 报错原文是 "cannot be dropped because some objects depend on it"。
            # 所以先 DROP OWNED BY 清掉它名下对象/权限，再 DROP ROLE —— 这是标准两步。
            sql = f'DROP OWNED BY {u}; DROP ROLE {u}'
        else:
            sql = f"DROP USER {u}@'%'"
        r = self.dbc.run_admin(c, sql, timeout=60)
        return {'ok': r['ok'], 'sql': sql, 'error': r['error']}

    def grant(self, cid: str, user: str, database: str, privs=None,
              schema: str = 'public') -> dict:
        c, err = self._conn_for_admin(cid)
        if err:
            return err
        allowed = self.dbc.GRANTABLE.get(c['kind'], ())
        want = [str(p).strip().upper() for p in (privs or ['ALL']) if str(p).strip()]
        bad = [p for p in want if p not in allowed]
        if bad:
            return {'ok': False, 'error': f'不允许的权限 {bad}；可选：{list(allowed)}'}
        try:
            u = self.dbc.quote_ident(c['kind'], user)
            db = self.dbc.check_ident(database, '库名')
        except ValueError as e:
            return {'ok': False, 'error': str(e)}
        joined = ', '.join(want)
        if c['kind'] == 'postgres':
            # PG 的权限分三层，写错哪层都是语法错误（真踩过：
            # `GRANT SELECT ON DATABASE x` → invalid privilege type SELECT for database）：
            #   库级 = CONNECT / TEMPORARY / CREATE
            #   模式级 = USAGE / CREATE
            #   表级 = SELECT / INSERT / UPDATE / DELETE / TRUNCATE / REFERENCES / TRIGGER
            db_level = [p for p in want if p in ('CONNECT', 'TEMPORARY', 'CREATE')]
            tbl_level = [p for p in want if p not in ('CONNECT', 'TEMPORARY', 'CREATE')
                         and p != 'USAGE']
            dbq = self.dbc.quote_ident(c['kind'], db)
            schq = self.dbc.quote_ident(c['kind'], schema)
            stmts = [f'GRANT CONNECT ON DATABASE {dbq} TO {u}']
            if db_level:
                stmts.append(f'GRANT {", ".join(db_level)} ON DATABASE {dbq} TO {u}')
            stmts.append(f'GRANT USAGE ON SCHEMA {schq} TO {u}')
            if tbl_level:
                stmts.append(f'GRANT {", ".join(tbl_level)} '
                             f'ON ALL TABLES IN SCHEMA {schq} TO {u}')
            sql = '; '.join(stmts)
        else:
            sql = f'GRANT {joined} ON `{db}`.* TO {u}'
        r = self.dbc.run_admin(c, sql, timeout=30)
        return {'ok': r['ok'], 'sql': sql, 'error': r['error']}

    def dump(self, cid: str, out_path: str, database: str = '') -> dict:
        c = self.get(cid, with_password=True)
        if c is None:
            return {'ok': False, 'error': '连接不存在'}
        r = self.dbc.dump(c, out_path, database)
        self._touch(cid, r.get('ok', False), r.get('error', ''))
        return r

    def restore(self, cid: str, path: str, database: str = '') -> dict:
        c = self.get(cid, with_password=True)
        if c is None:
            return {'ok': False, 'error': '连接不存在'}
        r = self.dbc.restore(c, path, database)
        self._touch(cid, r.get('ok', False), r.get('error', ''))
        return r

    # ── 数据源（随心跳上报，要轻量）─────────────────────────
    def provider_snapshot(self) -> dict:
        conns = self.list()['connections']
        by_kind = {}
        for c in conns:
            by_kind[c['kind']] = by_kind.get(c['kind'], 0) + 1
        drivers = {}
        try:
            scan = self.dbc.scan()
            drivers = {k: bool(v.get('client_available')) for k, v in scan.items()}
        except Exception:
            pass
        return {'count': len(conns), 'by_kind': by_kind,
                'clients': drivers, 'read_only': self.read_only,
                'key_fingerprint': self.key_fingerprint()}
