# -*- coding: utf-8 -*-
"""节点接口：多机管理的对外 REST（列表 / 纳管 / 命令 / 密钥轮换）。

内核在这一层只做「搬运」：数据来自各层注册的数据源，命令交给注册者执行。
接口本身不解释业务语义，因此新增运维能力无需改动本文件。
"""
import logging

from flask import request

from ..app import READONLY_CMDS

logger = logging.getLogger('zernus')


def register(app, ctx):
    fw = ctx.fw

    @app.route('/api/nodes', methods=['GET'])
    @ctx.require_auth
    def _list_nodes():
        """全部纳管节点（含在线态）；无纳管时返回本机。"""
        return ctx.ok({'nodes': fw.nodes.list(), 'mode': fw.nodes.mode})

    @app.route('/api/nodes/local', methods=['GET'])
    @ctx.require_auth
    def _local_node():
        """本机节点信息。"""
        return ctx.ok({'node': fw.nodes.local()})

    @app.route('/api/nodes/online', methods=['GET'])
    @ctx.require_auth
    def _online_nodes():
        """在线节点快照（控制面实时视图，不做网络请求）。"""
        return ctx.ok({'nodes': fw.nodes.snapshot()})

    @app.route('/api/nodes', methods=['POST'])
    @ctx.require_auth(role='super')
    def _add_node():
        """纳管节点；响应中的 secret 仅此一次返回，请立即保存到被管机器。"""
        data = request.get_json(silent=True) or {}
        res = fw.nodes.add(str(data.get('name') or ''),
                           host=str(data.get('host') or ''),
                           port=int(data.get('port') or 0),
                           tags=str(data.get('tags') or ''),
                           secret=data.get('secret'))
        if not res.get('ok'):
            return ctx.fail(res.get('error', '纳管失败'), 400)
        ctx.audit('node.add', 'node', res['name'])
        return ctx.ok(res)

    @app.route('/api/nodes/<name>', methods=['PATCH'])
    @ctx.require_auth(role='super')
    def _update_node(name):
        data = request.get_json(silent=True) or {}
        res = fw.nodes.update(name, **{k: data.get(k) for k in ('host', 'port', 'tags')
                                       if data.get(k) is not None})
        if not res.get('ok'):
            return ctx.fail(res.get('error', '更新失败'), 400)
        ctx.audit('node.update', 'node', name)
        return ctx.ok(res)

    @app.route('/api/nodes/<name>', methods=['DELETE'])
    @ctx.require_auth(role='super')
    def _remove_node(name):
        ctx.audit('node.remove', 'node', name)
        return ctx.ok(fw.nodes.remove(name))

    @app.route('/api/nodes/<name>/secret', methods=['POST'])
    @ctx.require_auth(role='super')
    def _rotate_secret(name):
        """轮换 pre-shared key；旧密钥立即失效，节点需同步更新配置。"""
        res = fw.nodes.rotate_secret(name)
        if not res.get('ok'):
            return ctx.fail(res.get('error', '轮换失败'), 404)
        ctx.audit('node.rotate_secret', 'node', name)
        return ctx.ok(res)

    @app.route('/api/nodes/<name>/cmd', methods=['POST'])
    @ctx.require_auth
    def _send_cmd(name):
        """下发命令。命令由服务层 / 软件层注册，内核只按名路由。

        viewer 角色只允许白名单内的只读命令（READONLY_CMDS）——
        认证只回答「是谁」，这里补上「能干什么」。
        """
        data = request.get_json(silent=True) or {}
        cmd = str(data.get('cmd') or '')
        if not cmd:
            return ctx.fail('缺少 cmd', 400)
        if request.zp_user.get('role') == 'viewer' and cmd not in READONLY_CMDS:
            ctx.audit('node.cmd.denied', 'node', name, detail={'cmd': cmd}, result='denied')
            return ctx.fail(f'viewer 角色只读，不允许执行 {cmd}', 403)
        timeout = float(data.get('timeout') or 30)
        res = fw.nodes.send_cmd(name, cmd, data.get('args') or {}, timeout)
        # 审计带 args（截断）—— 只有 cmd 名没有参数，取证时还原不了「写了哪个文件」
        ctx.audit('node.cmd', 'node', name,
                  detail={'cmd': cmd, 'args': data.get('args') or {}})
        if res.get('ok'):
            return ctx.ok(res)
        return ctx.fail(res.get('data', '命令执行失败'), 502)

    @app.route('/api/nodes/<name>/data', methods=['GET'])
    @ctx.require_auth
    def _node_data(name):
        """读取某节点最近一次上报的数据快照（来自库，不触发网络请求）。"""
        if str(name) in ('localhost', 'local', '本机'):
            return ctx.ok({'node': name, 'data': fw.nodes.collect()})
        rec = fw.nodes.registry.get(name)
        if not rec:
            return ctx.fail(f'节点 {name} 不存在', 404)
        import json
        detail = rec.get('detail')
        try:
            data = json.loads(detail) if detail else {}
        except Exception:
            data = {'raw': detail}
        return ctx.ok({'node': name, 'status': rec.get('status'), 'data': data})
