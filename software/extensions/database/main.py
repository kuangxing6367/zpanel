# -*- coding: utf-8 -*-
"""
数据库管理（官方扩展）

**管已有的数据库，绝不内置数据库、也不实现数据库协议。**

- 查询：调用本机/节点上**已装好**的官方客户端（`mysql` / `psql` / `redis-cli`），
  凭据不进命令行 —— 全部下沉在 `dbclient` 机制包里。
- 启停：交给**系统自己的服务管理器**（`svcmgr`），面板不自己拉起 mysqld。
- 口令：用 `secretbox` 加密后入库，接口只回 `has_password`，**永不下发明文**。

接入内核的方式与其它扩展一致：注册数据源（随心跳上报）+ 命令（可被 hub 下发到
任意节点）+ HTTP 接口。**内核完全不知道数据库的存在。**

安全边界（写死在这里，别被人"顺手放宽"）：
- 默认只允许只读语句（SELECT / SHOW / EXPLAIN / DESC / WITH…）。
  想放开要在 config 里显式关 `read_only`，关了也仍然禁止多语句（`;` 拼接）。
"""
import logging

logger = logging.getLogger('zernus')

__plugin_meta__ = {
    "name": "数据库管理",
    "version": "0.1.0",
    "author": "ZPanel",
    "desc": "管已有的 MySQL/PostgreSQL/Redis：库表查看、状态、进程、只读查询、服务启停",
    "priority": 40,
    "official": True,
}

_svc = None

# 各数据库的候选服务名（不同平台/发行版叫法不一样，逐个试）
SERVICE_CANDIDATES = {
    "mysql": ("mysqld", "mysql", "mariadb", "mysql80", "MySQL80"),
    "postgres": ("postgresql", "postgresql@16", "postgresql@15", "postgresql@14", "postgres"),
    "redis": ("redis", "redis-server", "redis6379"),
}

# 允许在「只读模式」下执行的前缀
READONLY_PREFIX = ("select", "show", "explain", "desc", "describe", "with", "pragma")


def _readonly(sql: str) -> tuple:
    """只读校验：返回 (ok, reason)。

    两道闸：① 必须是只读开头；② 不允许多语句（防 `SELECT 1; DROP ...`）。
    """
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        return False, "SQL 为空"
    if ";" in s:
        return False, "不允许一次执行多条语句（语句中包含分号）"
    head = s.split(None, 1)[0].lower()
    if head not in READONLY_PREFIX:
        return False, f"只读模式下不允许 {head.upper()} 语句"
    return True, ""


def register(ctx):
    """扩展入口：装配服务 → 注册数据源/命令 → 挂 HTTP 接口。"""
    global _svc
    fw = ctx._framework

    # 注意：官方扩展是按**文件路径**加载的（模块名 core_plugin_xxx），
    # 因此内部一律用绝对导入，相对导入会静默失败。
    from software.extensions.database.store import ConnectionStore

    dbc = ctx.zkg_tool('dbclient')
    sb = ctx.zkg_tool('secretbox')
    svc = ctx.zkg_tool('svcmgr')
    cmp_ = ctx.zkg_tool('compat')

    for name, mod in (('dbclient', dbc), ('secretbox', sb), ('svcmgr', svc),
                      ('compat', cmp_)):
        if mod is None:
            ctx.log(f"数据库管理：缺少机制包 {name}，已跳过（检查 manifest.toml 的 dependencies）")
            return

    cfg = fw.config.get('database') or {}
    _svc = ConnectionStore(fw, dbc, sb, svcmgr=svc, log=ctx.log,
                           read_only=bool(cfg.get('read_only', True)),
                           max_rows=int(cfg.get('max_rows', 500)),
                           compat=cmp_)
    fw.database_svc = _svc
    fw.services.register('database', _svc)

    # ── 数据源：随心跳上报（中心机能看到各节点的数据库连接概况）──
    fw.nodes.register_provider(
        'database',
        lambda: _svc.provider_snapshot(),
        desc='数据库连接与服务概况',
        level='software')

    def _ok(d):
        return {'ok': True, 'data': d}

    def _err(m):
        return {'ok': False, 'data': str(m)}

    # ── 命令处理器（远程节点管自己的数据库就靠这批）──
    def _h_scan(a):
        return _ok(_svc.scan())

    def _h_list(a):
        return _ok(_svc.list())

    def _h_create(a):
        r = _svc.create(a or {})
        return _ok(r) if r.get('ok') else _err(r.get('error'))

    def _h_update(a):
        r = _svc.update(a.get('id', ''), a or {})
        return _ok(r) if r.get('ok') else _err(r.get('error'))

    def _h_remove(a):
        r = _svc.remove(a.get('id', ''))
        return _ok(r) if r.get('ok') else _err(r.get('error'))

    def _h_test(a):
        return _ok(_svc.test(a.get('id', '')))

    def _h_databases(a):
        return _ok(_svc.databases(a.get('id', '')))

    def _h_tables(a):
        return _ok(_svc.tables(a.get('id', ''), a.get('database', '')))

    def _h_status(a):
        return _ok(_svc.db_status(a.get('id', '')))

    def _h_processlist(a):
        return _ok(_svc.processlist(a.get('id', '')))

    def _h_query(a):
        return _ok(_svc.query(a.get('id', ''), a.get('sql', '')))

    def _h_services(a):
        return _ok(_svc.services(a.get('kind', '')))

    def _h_service_act(a):
        return _ok(_svc.service_act(a.get('kind', ''), a.get('action', ''),
                                    a.get('name', '')))

    def _h_createdb(a):
        return _ok(_svc.create_database(a.get('id', ''), a.get('name', ''),
                                        a.get('owner', ''), a.get('encoding', '')))

    def _h_dropdb(a):
        return _ok(_svc.drop_database(a.get('id', ''), a.get('name', ''),
                                      a.get('confirm', '')))

    def _h_users(a):
        return _ok(_svc.users(a.get('id', '')))

    def _h_createuser(a):
        return _ok(_svc.create_user(a.get('id', ''), a.get('user', ''),
                                    a.get('password', ''),
                                    bool(a.get('superuser'))))

    def _h_dropuser(a):
        return _ok(_svc.drop_user(a.get('id', ''), a.get('user', ''),
                                  a.get('confirm', '')))

    def _h_grant(a):
        return _ok(_svc.grant(a.get('id', ''), a.get('user', ''),
                              a.get('database', ''), a.get('privs')))

    def _h_dump(a):
        return _ok(_svc.dump(a.get('id', ''), a.get('out_path', ''),
                             a.get('database', '')))

    def _h_restore(a):
        return _ok(_svc.restore(a.get('id', ''), a.get('path', ''),
                                a.get('database', '')))

    def _h_compat(a):
        """版本兼容层：按服务端真实版本给出所属层、与上一版的差异、注意事项。"""
        return _ok(_svc.compat_profile(a.get('id', '')))

    def _h_serverver(a):
        return _ok(_svc.server_version(a.get('id', '')))

    for name, fn, desc in (
        ('db.scan', _h_scan, '探测数据库客户端/服务端'),
        ('db.list', _h_list, '连接清单'),
        ('db.create', _h_create, '新建连接'),
        ('db.update', _h_update, '修改连接'),
        ('db.remove', _h_remove, '删除连接'),
        ('db.test', _h_test, '连通性测试'),
        ('db.databases', _h_databases, '库清单'),
        ('db.tables', _h_tables, '表清单'),
        ('db.status', _h_status, '运行状态'),
        ('db.processlist', _h_processlist, '活动连接'),
        ('db.query', _h_query, '只读查询'),
        ('db.services', _h_services, '数据库服务状态'),
        ('db.service.act', _h_service_act, '启停数据库服务'),
        ('db.createdb', _h_createdb, '新建数据库'),
        ('db.dropdb', _h_dropdb, '删除数据库（需 confirm 同名）'),
        ('db.users', _h_users, '用户/角色清单'),
        ('db.createuser', _h_createuser, '新建数据库用户'),
        ('db.dropuser', _h_dropuser, '删除用户（需 confirm 同名）'),
        ('db.grant', _h_grant, '授权'),
        ('db.dump', _h_dump, '导出为 SQL 文件'),
        ('db.restore', _h_restore, '从 SQL 文件导入'),
        ('db.version', _h_serverver, '服务端版本'),
        ('db.compat', _h_compat, '版本兼容层档案（层/差异/注意事项）'),
    ):
        fw.nodes.register_handler(name, fn, desc=desc, level='software')

    # ── HTTP 接口 ──────────────────────────────────────────
    from flask import jsonify, request as _req

    def _body():
        return _req.get_json(silent=True) or {}

    def _api_scan():
        return jsonify({'ok': True, **_svc.scan()})

    def _api_list():
        r = _svc.list()
        return jsonify({'ok': True, **r})

    def _api_create():
        r = _svc.create(_body())
        if not r.get('ok'):
            return jsonify({'ok': False, 'error': r.get('error')}), 400
        return jsonify({'ok': True, 'conn': r['conn']})

    def _api_update(cid):
        r = _svc.update(cid, _body())
        if not r.get('ok'):
            return jsonify({'ok': False, 'error': r.get('error')}), 400
        return jsonify({'ok': True, 'conn': r['conn']})

    def _api_remove(cid):
        r = _svc.remove(cid)
        return (jsonify({'ok': True}) if r.get('ok')
                else (jsonify({'ok': False, 'error': r.get('error')}), 404))

    def _api_test(cid):
        return jsonify({'ok': True, **_svc.test(cid)})

    def _api_databases(cid):
        return jsonify({'ok': True, **_svc.databases(cid)})

    def _api_tables(cid):
        return jsonify({'ok': True, **_svc.tables(cid, _req.args.get('database', ''))})

    def _api_status(cid):
        return jsonify({'ok': True, **_svc.db_status(cid)})

    def _api_processlist(cid):
        return jsonify({'ok': True, **_svc.processlist(cid)})

    def _api_compat(cid):
        r = _svc.compat_profile(cid)
        if not r.get('ok'):
            return jsonify({'ok': False, 'error': r.get('error')}), 502
        return jsonify({'ok': True, **r})

    def _api_serverver(cid):
        r = _svc.server_version(cid)
        if not r.get('ok'):
            return jsonify({'ok': False, 'error': r.get('error')}), 502
        return jsonify({'ok': True, **r})

    def _api_query(cid):
        r = _svc.query(cid, (_body().get('sql') or _req.args.get('sql') or ''))
        if not r.get('ok'):
            return jsonify({'ok': False, 'error': r.get('error')}), 400
        return jsonify({'ok': True, **r})

    def _api_services():
        return jsonify({'ok': True, **_svc.services(_req.args.get('kind', ''))})

    def _api_service_act():
        b = _body()
        r = _svc.service_act(b.get('kind', ''), b.get('action', ''), b.get('name', ''))
        return (jsonify({'ok': True, **r}) if r.get('ok')
                else (jsonify({'ok': False, **r}), 502))

    def _api_createdb(cid):
        r = _svc.create_database(cid, (_body().get('name') or ''),
                                 (_body().get('owner') or ''), (_body().get('encoding') or ''))
        return (jsonify({'ok': True, **r}) if r.get('ok')
                else (jsonify({'ok': False, **r}), 400))

    def _api_dropdb(cid):
        b = _body()
        r = _svc.drop_database(cid, b.get('name', ''), b.get('confirm', ''))
        return (jsonify({'ok': True, **r}) if r.get('ok')
                else (jsonify({'ok': False, **r}), 400))

    def _api_users(cid):
        return jsonify({'ok': True, **_svc.users(cid)})

    def _api_createuser(cid):
        b = _body()
        r = _svc.create_user(cid, b.get('user', ''), b.get('password', ''),
                             bool(b.get('superuser')))
        return (jsonify({'ok': True, **r}) if r.get('ok')
                else (jsonify({'ok': False, **r}), 400))

    def _api_dropuser(cid):
        b = _body()
        r = _svc.drop_user(cid, b.get('user', ''), b.get('confirm', ''))
        return (jsonify({'ok': True, **r}) if r.get('ok')
                else (jsonify({'ok': False, **r}), 400))

    def _api_grant(cid):
        b = _body()
        r = _svc.grant(cid, b.get('user', ''), b.get('database', ''), b.get('privs'))
        return (jsonify({'ok': True, **r}) if r.get('ok')
                else (jsonify({'ok': False, **r}), 400))

    def _api_dump(cid):
        b = _body()
        r = _svc.dump(cid, b.get('out_path', ''), b.get('database', ''))
        return (jsonify({'ok': True, **r}) if r.get('ok')
                else (jsonify({'ok': False, **r}), 400))

    def _api_restore(cid):
        b = _body()
        r = _svc.restore(cid, b.get('path', ''), b.get('database', ''))
        return (jsonify({'ok': True, **r}) if r.get('ok')
                else (jsonify({'ok': False, **r}), 400))

    ctx.register_api('/api/db/scan', _api_scan, methods=['GET'], description='探测客户端')
    ctx.register_api('/api/db', _api_list, methods=['GET'], description='连接清单')
    ctx.register_api('/api/db', _api_create, methods=['POST'], description='新建连接')
    ctx.register_api('/api/db/<cid>', _api_update, methods=['PATCH'])
    ctx.register_api('/api/db/<cid>', _api_remove, methods=['DELETE'])
    ctx.register_api('/api/db/<cid>/test', _api_test, methods=['POST'])
    ctx.register_api('/api/db/<cid>/databases', _api_databases, methods=['GET'])
    ctx.register_api('/api/db/<cid>/tables', _api_tables, methods=['GET'])
    ctx.register_api('/api/db/<cid>/status', _api_status, methods=['GET'])
    ctx.register_api('/api/db/<cid>/processlist', _api_processlist, methods=['GET'])
    ctx.register_api('/api/db/<cid>/version', _api_serverver, methods=['GET'],
                     description='服务端版本')
    ctx.register_api('/api/db/<cid>/compat', _api_compat, methods=['GET'],
                     description='版本兼容层档案（命中层/与上一版差异/注意事项）')
    ctx.register_api('/api/db/<cid>/query', _api_query, methods=['POST'])
    ctx.register_api('/api/db/services', _api_services, methods=['GET'],
                     description='数据库服务状态')
    ctx.register_api('/api/db/services/act', _api_service_act, methods=['POST'],
                     description='启停数据库服务')
    ctx.register_api('/api/db/<cid>/createdb', _api_createdb, methods=['POST'],
                     description='新建数据库')
    ctx.register_api('/api/db/<cid>/dropdb', _api_dropdb, methods=['POST'],
                     description='删除数据库（需 confirm）')
    ctx.register_api('/api/db/<cid>/users', _api_users, methods=['GET'],
                     description='用户/角色清单')
    ctx.register_api('/api/db/<cid>/createuser', _api_createuser, methods=['POST'],
                     description='新建用户')
    ctx.register_api('/api/db/<cid>/dropuser', _api_dropuser, methods=['POST'],
                     description='删除用户（需 confirm）')
    ctx.register_api('/api/db/<cid>/grant', _api_grant, methods=['POST'],
                     description='授权')
    ctx.register_api('/api/db/<cid>/dump', _api_dump, methods=['POST'],
                     description='导出为 SQL 文件')
    ctx.register_api('/api/db/<cid>/restore', _api_restore, methods=['POST'],
                     description='从 SQL 文件导入')

    ctx.log(f"数据库管理已就绪：{_svc.list()['count']} 个连接；"
            f"只读模式 {'开' if _svc.read_only else '关'}")


def unregister():
    global _svc
    _svc = None
