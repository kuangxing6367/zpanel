# -*- coding: utf-8 -*-
"""
运行时管理（官方扩展）—— PHP / Node.js / Java 等服务的实例化托管

内核不管系统环境，所以本扩展落在**软件层**，通过内核提供的两个注册点接入：

    fw.nodes.register_provider('runtime', fn)          # 数据源：随心跳上报
    fw.nodes.register_handler('runtime.start', fn)     # 命令：可被 hub 下发执行

于是**多机能力是白拿的**——hub 侧一句 `send_cmd('node-1', 'runtime.start', {...})`
就能拉起那台机器上的实例，内核一行都不用改。实例输出也直接走
`0x12 STDOUT` 流帧，不占 Web API。

对外提供两套入口，能力等价：
- **HTTP API**（面板用）：`/api/runtime/*`，挂在内核 API 上、复用内核鉴权；
- **内核命令**（远程机器用）：`runtime.*`，供 hub 下发到本节点执行。
"""
import logging
import threading

logger = logging.getLogger('zernus')

__plugin_meta__ = {
    "name": "运行时管理",
    "version": "0.1.0",
    "author": "ZPanel",
    "desc": "PHP / Node.js / Java 等服务的实例化托管与生命周期管理",
    "priority": 30,
    "official": True,
}

_subsystem = None


def register(ctx):
    """装载实例子系统，并注册数据源、命令与 HTTP 接口。"""
    global _subsystem
    fw = ctx._framework
    cfg = fw.config.get('runtime') or {}
    if cfg.get('enabled', True) is False:
        ctx.log("运行时管理已禁用 (runtime.enabled: false)")
        return

    # 注意：官方扩展是按**文件路径**加载的（模块名为 core_plugin_xxx），
    # 因此内部一律用绝对导入，相对导入会失败。
    from software.extensions.runtime.subsystem import RuntimeSubsystem
    from software.extensions.runtime import adapters, instance as instance_mod

    # ── 机制包注入（依赖已在 manifest.toml 声明，取不到就是依赖图坏了）──
    # probe：探测本机运行时（多候选名搜索 + 版本解析 + TTL 缓存）
    # procs：进程创建与整树终止的唯一实现处
    # compat：版本兼容层（一个版本一个层 —— 路径约定/服务名/能力开关/注意事项）
    probe, procs = ctx.zkg_tool('probe'), ctx.zkg_tool('procs')
    if probe is None or procs is None:
        raise RuntimeError(
            "运行时管理依赖机制包 probe / procs，但未加载 —— "
            "检查 software/extensions/runtime/manifest.toml 的 dependencies，"
            "以及 zkg 是否扫描到了 repo/（已加载: "
            f"{ctx.zkg_tool('probe') and 'probe' or '—'}, "
            f"{ctx.zkg_tool('procs') and 'procs' or '—'}）")
    if ctx.zkg_tool('compat') is None:
        raise RuntimeError(
            "运行时管理依赖机制包 compat，但未加载 —— "
            "检查 software/extensions/runtime/manifest.toml 的 dependencies，"
            "并确认已执行 repo/build.py 重建索引")
    adapters.bind(ctx.zkg_tool)
    instance_mod.bind(procs)

    _subsystem = RuntimeSubsystem(fw, log=ctx.log)
    fw.runtime = _subsystem                     # 供内核与其它扩展直接取用
    fw.services.register('runtime', _subsystem)
    _subsystem.ensure_table()
    _subsystem.load_all()

    # ── 实时输出桥：实例的每一行输出 → 内核实时推送（fw.stream）──
    #    fw.stream 由内核 Web 初始化（core/api/stream.py），扩展加载早于它，
    #    所以这里延迟取用；topic = instance.<id>，前端按实例订阅。
    def _output_bridge(iid, stream, line):
        st = getattr(fw, 'stream', None)
        if st is not None:
            st.publish(f'instance.{iid}', 'output',
                       {'id': iid, 'stream': stream, 'line': line})

    _subsystem.set_output_bridge(_output_bridge)

    # ══════════════════════════════════════════════════════
    # ① 数据源：实例清单摘要（随心跳上报给 hub）
    # ══════════════════════════════════════════════════════
    def _provider():
        return {
            'count': _subsystem.count(),
            'instances': [
                {'id': i['id'], 'name': i['name'], 'kind': i['kind'],
                 'status': i['status'], 'port': i['port'],
                 'uptime_seconds': i['uptime_seconds'], 'pid': i['pid']}
                for i in _subsystem.list()
            ],
        }

    fw.nodes.register_provider('runtime', _provider, desc='运行时实例清单', level='software')

    # ══════════════════════════════════════════════════════
    # ② 命令：供 hub 下发到本节点执行（远程多机的落点）
    # ══════════════════════════════════════════════════════
    def _ok(data):
        return {'ok': True, 'data': data}

    def _err(msg):
        return {'ok': False, 'data': str(msg)}

    def _h_list(args):
        return _ok({'instances': _subsystem.list(), 'count': _subsystem.count()})

    def _h_create(args):
        res = _subsystem.create(args or {})
        return _ok(res['instance']) if res.get('ok') else _err(res.get('error'))

    def _h_start(args):
        res = _subsystem.start((args or {}).get('id', ''))
        return _ok({'started': True}) if res.get('ok') else _err(res.get('error'))

    def _h_stop(args):
        res = _subsystem.stop((args or {}).get('id', ''),
                              force=bool((args or {}).get('force')))
        return _ok({'stopped': True}) if res.get('ok') else _err(res.get('error'))

    def _h_restart(args):
        res = _subsystem.restart((args or {}).get('id', ''))
        return _ok({'restarted': True}) if res.get('ok') else _err(res.get('error'))

    def _h_logs(args):
        a = args or {}
        res = _subsystem.logs(a.get('id', ''), int(a.get('limit') or 200))
        return _ok(res.get('lines', [])) if res.get('ok') else _err(res.get('error'))

    def _h_remove(args):
        res = _subsystem.remove((args or {}).get('id', ''))
        return _ok({'removed': True}) if res.get('ok') else _err(res.get('error'))

    def _h_detect(args):
        return _ok({'runtimes': adapters.detect_runtimes()})

    def _h_versions(args):
        """多版本清点：这台机器上到底装了哪几个版本、分别在哪。"""
        kinds = (args or {}).get('kinds')
        if isinstance(kinds, str):
            kinds = [k.strip() for k in kinds.split(',') if k.strip()]
        return _ok({'kinds': adapters.detect_versions(kinds)})

    def _h_env(args):
        """环境体检（只读）：装没装 / 包管理器与关键路径是否就位。"""
        return _ok(adapters.env_report())

    def _layer_brief(items):
        """层的精简视图：全量 caps 只在问具体版本时才给，避免清单接口过大。"""
        return [{'id': x['id'], 'family': x['family'], 'title': x.get('title', ''),
                 'match': x.get('match', '')} for x in items]

    def _compat_view(a):
        """版本兼容层视图（命令与 HTTP 共用一份实现）。

        - 只给 family        → 这一族的所有层（id / 管的版本区间 / 标题）
        - 给 family+version  → 命中哪一层 + 相对上一层多/少了什么 + 注意事项
        - 什么都不给          → 家族概览（每族几层）+ 层的清单
        """
        c = adapters.compat()
        fam = str(a.get('family') or '').strip()
        ver = str(a.get('version') or '').strip()
        if fam and ver:
            return {'explain': c.explain(fam, ver)}
        if fam:
            return {'family': fam, 'layers': _layer_brief(c.layers(fam))}
        return {'families': c.families(), 'layers': _layer_brief(c.layers())}

    def _h_compat(args):
        return _ok(_compat_view(args or {}))

    def _h_template(args):
        a = args or {}
        return _ok({'start_command': adapters.command_template(
            a.get('kind', 'generic'),
            {'entry': a.get('entry', ''), 'cwd': a.get('cwd', ''),
             'args': a.get('args', ''), 'port': a.get('port', 0),
             'exe': a.get('exe', ''), 'jvm_args': a.get('jvm_args', '')})})

    for name, fn, desc in (
        ('runtime.list', _h_list, '实例清单'),
        ('runtime.create', _h_create, '创建实例'),
        ('runtime.start', _h_start, '启动实例'),
        ('runtime.stop', _h_stop, '停止实例'),
        ('runtime.restart', _h_restart, '重启实例'),
        ('runtime.logs', _h_logs, '读取实例输出'),
        ('runtime.remove', _h_remove, '删除实例'),
        ('runtime.detect', _h_detect, '探测本机运行时'),
        ('runtime.versions', _h_versions, '多版本清点'),
        ('runtime.env', _h_env, '运行时环境体检'),
        ('runtime.compat', _h_compat, '版本兼容层（版本 → 能力/路径/注意事项）'),
        ('runtime.template', _h_template, '生成启动命令模板'),
    ):
        fw.nodes.register_handler(name, fn, desc=desc, level='software')

    # ══════════════════════════════════════════════════════
    # ③ HTTP API（面板用）
    # ══════════════════════════════════════════════════════
    from flask import jsonify, request as _req

    def _body():
        return _req.get_json(silent=True) or {}

    def _api_list():
        return jsonify({'ok': True, 'instances': _subsystem.list(),
                        'count': _subsystem.count()})

    def _api_create():
        res = _subsystem.create(_body())
        if not res.get('ok'):
            return jsonify({'ok': False, 'error': res.get('error')}), 400
        return jsonify({'ok': True, 'instance': res['instance']})

    def _api_update(iid):
        res = _subsystem.update(iid, _body())
        if not res.get('ok'):
            return jsonify({'ok': False, 'error': res.get('error')}), 404
        return jsonify({'ok': True, 'instance': res['instance']})

    def _api_remove(iid):
        res = _subsystem.remove(iid)
        return (jsonify({'ok': True}) if res.get('ok')
                else (jsonify({'ok': False, 'error': res.get('error')}), 404))

    def _api_action(iid, action):
        res = _subsystem._act(iid, action, **_body().get('kwargs', {}))
        if not res.get('ok'):
            return jsonify({'ok': False, 'error': res.get('error')}), 400
        return jsonify({'ok': True, 'instance': _subsystem.get(iid).snapshot()})

    def _api_start(iid):
        return _api_action(iid, 'start')

    def _api_stop(iid):
        return _api_action(iid, 'stop')

    def _api_restart(iid):
        return _api_action(iid, 'restart')

    def _api_logs(iid):
        limit = int(_req.args.get('limit') or 200)
        res = _subsystem.logs(iid, limit)
        if not res.get('ok'):
            return jsonify({'ok': False, 'error': res.get('error')}), 404
        return jsonify({'ok': True, 'lines': res['lines']})

    def _api_stdin(iid):
        res = _subsystem.write_stdin(iid, str(_body().get('data') or ''))
        return (jsonify({'ok': True}) if res.get('ok')
                else (jsonify({'ok': False, 'error': res.get('error')}), 400))

    def _api_detect():
        return jsonify({'ok': True, 'runtimes': adapters.detect_runtimes()})

    def _api_versions():
        kinds = _req.args.get('kinds')
        return jsonify({'ok': True,
                        'kinds': adapters.detect_versions(
                            [k.strip() for k in kinds.split(',')] if kinds else None)})

    def _api_env():
        return jsonify({'ok': True, **adapters.env_report()})

    def _api_compat():
        a = {'family': _req.args.get('family', ''),
             'version': _req.args.get('version', '')}
        return jsonify({'ok': True, **_compat_view(a)})

    def _api_template():
        a = _req.args
        cmd = adapters.command_template(
            a.get('kind', 'generic'),
            {'entry': a.get('entry', ''), 'cwd': a.get('cwd', ''),
             'args': a.get('args', ''), 'port': a.get('port', 0)})
        return jsonify({'ok': True, 'start_command': cmd})

    ctx.register_api('/api/runtime/instances', _api_list, methods=['GET'],
                     description='实例清单')
    ctx.register_api('/api/runtime/instances', _api_create, methods=['POST'],
                     description='创建实例')
    ctx.register_api('/api/runtime/instances/<iid>', _api_update, methods=['PATCH'])
    ctx.register_api('/api/runtime/instances/<iid>', _api_remove, methods=['DELETE'])
    ctx.register_api('/api/runtime/instances/<iid>/start', _api_start, methods=['POST'])
    ctx.register_api('/api/runtime/instances/<iid>/stop', _api_stop, methods=['POST'])
    ctx.register_api('/api/runtime/instances/<iid>/restart', _api_restart, methods=['POST'])
    ctx.register_api('/api/runtime/instances/<iid>/logs', _api_logs, methods=['GET'])
    ctx.register_api('/api/runtime/instances/<iid>/stdin', _api_stdin, methods=['POST'])
    ctx.register_api('/api/runtime/detect', _api_detect, methods=['GET'],
                     description='探测本机运行时')
    ctx.register_api('/api/runtime/versions', _api_versions, methods=['GET'],
                     description='多版本清点（列出机器上所有版本）')
    ctx.register_api('/api/runtime/env', _api_env, methods=['GET'],
                     description='运行时环境体检（只读）')
    ctx.register_api('/api/runtime/compat', _api_compat, methods=['GET'],
                     description='版本兼容层：family[+version] → 命中层与注意事项')
    ctx.register_api('/api/runtime/template', _api_template, methods=['GET'],
                     description='生成启动命令模板')

    ctx.log(f"运行时管理已就绪：{_subsystem.count()['total']} 个实例；"
            f"数据源 runtime + 命令 runtime.* 已注册进内核")

    # ══════════════════════════════════════════════════════
    # ④ 自启（错峰，避免启动风暴）
    # ══════════════════════════════════════════════════════
    if cfg.get('auto_start', True):
        threading.Thread(target=_subsystem.auto_start_all,
                         name='runtime-autostart', daemon=True).start()


def unregister():
    """卸载：停掉所有实例（避免留下孤儿进程）。"""
    global _subsystem
    if _subsystem is not None:
        try:
            n = _subsystem.stop_all()
            logger.info("[runtime] 已停止 %d 个实例", n)
        except Exception as e:
            logger.warning("[runtime] 卸载清理异常: %s", e)
        _subsystem = None
