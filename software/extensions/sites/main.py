# -*- coding: utf-8 -*-
"""
站点托管（官方扩展）

「一个站点 = 域名 + 根目录 + 后端方式」。与实例解耦：
`proxy` 站点把域名流量反代到实例端口，所以「建站」和「跑服务」可以分开做。

**双后端**：装了 Nginx 就下发 conf 并 reload；没装就用内置服务兜底 ——
面板上的操作完全一致，不需要为了建第一个站先装一堆东西。

接入内核的方式与 runtime 一致（数据源 + 命令），所以站点清单同样随心跳上报，
远程节点也能通过 `sites.*` 命令管自己的站点。
"""
import logging

logger = logging.getLogger('zernus')

__plugin_meta__ = {
    "name": "站点托管",
    "version": "0.1.0",
    "author": "ZPanel",
    "desc": "站点（域名 + 根目录 + 静态/PHP/反代），双后端：Nginx 或内置服务",
    "priority": 35,
    "official": True,
}

_subsystem = None


def register(ctx):
    global _subsystem
    fw = ctx._framework
    cfg = fw.config.get('sites') or {}
    if cfg.get('enabled', True) is False:
        ctx.log("站点托管已禁用 (sites.enabled: false)")
        return

    from software.extensions.sites.subsystem import SiteSubsystem

    _subsystem = SiteSubsystem(fw, log=ctx.log)
    fw.sites = _subsystem
    fw.services.register('sites', _subsystem)
    _subsystem.ensure_table()
    _subsystem.load_all()

    # ── 数据源 ──
    fw.nodes.register_provider(
        'sites',
        lambda: {'count': _subsystem.count(),
                 'backend': (_subsystem.webserver_select().get('active') or {}).get('id') or 'builtin',
                 'sites': [{'id': s['id'], 'name': s['name'],
                            'domains': s['domains'], 'kind': s['kind']}
                           for s in _subsystem.list()]},
        desc='站点清单', level='software')

    # ── 命令（远程节点据此管自己的站点）──
    def _ok(d):
        return {'ok': True, 'data': d}

    def _err(m):
        return {'ok': False, 'data': str(m)}

    def _h_list(args):
        return _ok({'sites': _subsystem.list(), 'count': _subsystem.count()})

    def _h_create(args):
        r = _subsystem.create(args or {})
        return _ok(r['site']) if r.get('ok') else _err(r.get('error'))

    def _h_update(args):
        a = args or {}
        r = _subsystem.update(a.get('id', ''), a)
        return _ok(r['site']) if r.get('ok') else _err(r.get('error'))

    def _h_remove(args):
        r = _subsystem.remove((args or {}).get('id', ''))
        return _ok({'removed': True}) if r.get('ok') else _err(r.get('error'))

    def _h_status(args):
        return _ok(_subsystem.snapshot())

    def _h_render(args):
        a = args or {}
        conf = _subsystem.preview_nginx(a.get('id', ''))
        if conf is None:
            return _err('站点不存在')
        return _ok({'id': a.get('id', ''), 'nginx_conf': conf})

    def _h_php_versions(args):
        return _ok({'versions': _subsystem.php_versions()})

    for name, fn, desc in (
        ('sites.list', _h_list, '站点清单'),
        ('sites.create', _h_create, '创建站点'),
        ('sites.update', _h_update, '更新站点'),
        ('sites.remove', _h_remove, '删除站点'),
        ('sites.status', _h_status, '站点后端状态'),
        ('sites.render', _h_render, '渲染 Nginx 配置'),
        ('sites.adopt.scan', lambda a: _ok(_subsystem.adopt_scan()), '收养扫描（现存 server 块清单）'),
        ('sites.php-versions', _h_php_versions, '本机 PHP 版本清单'),
    ):
        fw.nodes.register_handler(name, fn, desc=desc, level='software')

    # ── HTTP API ──
    from flask import jsonify, request as _req

    def _body():
        return _req.get_json(silent=True) or {}

    def _api_list():
        return jsonify({'ok': True, 'sites': _subsystem.list(),
                        'count': _subsystem.count()})

    def _api_status():
        return jsonify({'ok': True, **_subsystem.snapshot()})

    def _api_create():
        r = _subsystem.create(_body())
        if not r.get('ok'):
            return jsonify({'ok': False, 'error': r.get('error')}), 400
        return jsonify({'ok': True, 'site': r['site'], 'apply': r.get('apply')})

    def _api_update(sid):
        r = _subsystem.update(sid, _body())
        if not r.get('ok'):
            return jsonify({'ok': False, 'error': r.get('error')}), 400
        return jsonify({'ok': True, 'site': r['site']})

    def _api_remove(sid):
        r = _subsystem.remove(sid)
        return (jsonify({'ok': True}) if r.get('ok')
                else (jsonify({'ok': False, 'error': r.get('error')}), 404))

    def _api_apply():
        return jsonify({'ok': True, **_subsystem.apply()})

    def _api_preview(sid):
        conf = _subsystem.preview_nginx(sid)
        if conf is None:
            return jsonify({'ok': False, 'error': '站点不存在'}), 404
        return jsonify({'ok': True, 'nginx_conf': conf})

    def _api_php_versions():
        return jsonify({'ok': True, 'versions': _subsystem.php_versions()})

    ctx.register_api('/api/sites', _api_list, methods=['GET'], description='站点清单')
    ctx.register_api('/api/sites', _api_create, methods=['POST'], description='创建站点')
    ctx.register_api('/api/sites/status', _api_status, methods=['GET'])
    ctx.register_api('/api/sites/apply', _api_apply, methods=['POST'])
    ctx.register_api('/api/sites/php-versions', _api_php_versions, methods=['GET'],
                     description='本机 PHP 版本清单（建站下拉用）')
    ctx.register_api('/api/sites/<sid>', _api_update, methods=['PATCH'])
    ctx.register_api('/api/sites/<sid>', _api_remove, methods=['DELETE'])
    ctx.register_api('/api/sites/<sid>/nginx', _api_preview, methods=['GET'],
                     description='预览站点配置（跟随当前引擎）')

    # 启动时下发一次（把库里的站点重新装进当前生效的 web 服务器/内置服务）
    res = _subsystem.apply()
    _ws_active = (res.get('webserver') or {}).get('active') or {}
    ctx.log(f"站点托管已就绪：{_subsystem.count()['total']} 个站点；"
            f"引擎={_ws_active.get('name') or '无（仅生成配置）'}"
            f"{'（已启动 ' + _subsystem.server.host + ':' + str(_subsystem.server.port) + '）' if res.get('builtin_running') else ''}")


def unregister():
    global _subsystem
    if _subsystem is not None:
        try:
            _subsystem.server.stop()
        except Exception as e:
            logger.warning("[sites] 停止内置站点服务异常: %s", e)
        _subsystem = None
