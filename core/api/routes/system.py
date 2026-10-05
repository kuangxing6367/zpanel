# -*- coding: utf-8 -*-
"""系统接口：健康探活 / 内核元信息 / 已注册的数据源与命令。"""
import logging
import os
import time

import yaml
from flask import request

logger = logging.getLogger('zernus')


def register(app, ctx):
    fw = ctx.fw

    @app.route('/api/system/health', methods=['GET'])
    def _health():
        """探活（无需鉴权，恒 200，供监控 / 负载均衡使用）。"""
        return ctx.ok({
            'status': 'ok',
            'time': time.strftime('%Y-%m-%d %H:%M:%S'),
            'api': 'zpanel-core',
        })

    @app.route('/api/system/info', methods=['GET'])
    @ctx.require_auth
    def _info():
        """内核元信息 —— 直接取内核数据源，不加工、不猜测。

        ``zkg`` 一段来自服务层（zkg 包管理把加载全貌挂在框架上），
        内核不解释这些字段，只如实转述 —— 与数据源/命令清单同一个原则。
        """
        return ctx.ok({
            'core': fw.nodes.collect().get('core', {}),
            'mode': fw.nodes.mode,
            'providers': fw.nodes.providers(),
            'handlers': fw.nodes.handlers(),
            'nodes': len(fw.nodes.list()),
            'zkg': getattr(fw, 'zkg_info', None) or {},
        })

    @app.route('/api/system/packages', methods=['GET'])
    @ctx.require_auth
    def _packages():
        """zkg 机制包的加载全貌（服务层提供，内核只转述）。

        回答三个问题：按依赖加载了哪些包、哪些因**无人依赖被剪枝**、哪些依赖缺失。
        """
        info = dict(getattr(fw, 'zkg_info', None) or {})
        info['tools'] = sorted((getattr(fw, 'zkg_tools', None) or {}).keys())
        return ctx.ok({'packages': info})

    @app.route('/api/system/update-check', methods=['GET'])
    @ctx.require_auth
    def _update_check():
        """升级检查：本地 VERSION 对比 GitHub main 的 VERSION（网络不通则如实报错）。"""
        import urllib.request
        local = ''
        try:
            from core.kernel.paths import project_root
            with open(os.path.join(project_root(), 'VERSION'), encoding='utf-8') as f:
                local = f.read().strip()
        except Exception:
            pass
        remote, err = '', ''
        try:
            url = 'https://raw.githubusercontent.com/kuangxing6367/zpanel/main/VERSION'
            with urllib.request.urlopen(url, timeout=10) as resp:
                remote = resp.read().decode('utf-8', 'replace').strip()
        except Exception as e:
            err = f'{type(e).__name__}: {e}'
        return ctx.ok({'local': local, 'remote': remote,
                       'update_available': bool(remote and remote != local and not err),
                       'error': err})

    @app.route('/api/tasks', methods=['GET'])
    @ctx.require_auth
    def _tasks():
        """本机任务列表（内核任务队列快照，含状态统计）。

        这里只暴露 **hub 本机** 的任务 —— 远程节点的任务在那台节点的
        队列里，经命令通道 task.list / task.get 查询。
        """
        tq = getattr(fw, 'task_queue', None)
        if tq is None:
            return ctx.ok({'tasks': [], 'stats': {}})
        state = (request.args.get('state') or '').strip() or None
        try:
            limit = max(1, min(int(request.args.get('limit') or 100), 500))
        except ValueError:
            limit = 100
        mem = tq.list_tasks(state=state, limit=limit)
        # 合并历史表：重启前完成的任务仍有账可查（内存里没有 id 的）
        mem_ids = {t['id'] for t in mem}
        history = []
        try:
            import json
            cond = "WHERE state=? " if state else ""
            rows = fw.db.query(
                f"SELECT * FROM task_history {cond}ORDER BY finished_at DESC LIMIT ?",
                ((state,) if state else ()) + (limit,))
            for r in rows or []:
                if r['id'] in mem_ids:
                    continue
                try:
                    log = json.loads(r['log'] or '[]')
                except Exception:
                    log = []
                try:
                    result = json.loads(r['result']) if r['result'] else None
                except Exception:
                    result = None
                try:
                    meta = json.loads(r['meta'] or '{}')
                except Exception:
                    meta = {}
                history.append({'id': r['id'], 'name': r['name'], 'state': r['state'],
                                'meta': meta, 'log': log, 'error': r['error'],
                                'result': result, 'submitted_at': r['submitted_at'],
                                'started_at': r['started_at'],
                                'finished_at': r['finished_at']})
        except Exception:
            pass  # 历史表尚未创建（新装）——如实空着
        merged = sorted(mem + history,
                        key=lambda t: t.get('submitted_at') or 0, reverse=True)[:limit]
        return ctx.ok({'tasks': merged, 'stats': tq.stats()})

    @app.route('/api/tasks/<task_id>', methods=['GET'])
    @ctx.require_auth
    def _task_detail(task_id):
        tq = getattr(fw, 'task_queue', None)
        task = tq.get_task(task_id) if tq else None
        if task is None:
            return ctx.fail(f'任务不存在: {task_id}', 404)
        return ctx.ok(task)

    @app.route('/api/tasks/<task_id>', methods=['DELETE'])
    @ctx.require_auth
    def _task_cancel(task_id):
        """取消任务。只有还在排队的能取消 —— 执行中的如实拒绝（进程型任务无法中断）。"""
        tq = getattr(fw, 'task_queue', None)
        if tq is None:
            return ctx.fail('任务队列不可用', 503)
        r = tq.cancel(task_id)
        if not r.get('ok'):
            return ctx.fail(str(r.get('error') or '无法取消'), 409)
        return ctx.ok(r)

    @app.route('/api/system/providers', methods=['GET'])
    @ctx.require_auth
    def _providers():
        """各层注册的数据源清单（服务层 / 软件层）。"""
        return ctx.ok({'providers': fw.nodes.providers()})

    @app.route('/api/system/handlers', methods=['GET'])
    @ctx.require_auth
    def _handlers():
        """各层注册的命令清单。"""
        return ctx.ok({'handlers': fw.nodes.handlers()})

    @app.route('/api/system/config', methods=['GET'])
    @ctx.require_auth
    def _config():
        """回显配置（对密钥类字段脱敏）。"""
        secret_keys = {'token', 'secret', 'password', 'access_token', 'api_key'}

        def _mask(obj):
            if isinstance(obj, dict):
                return {k: ('***' if k in secret_keys and obj[k] else _mask(v))
                        for k, v in obj.items()}
            if isinstance(obj, list):
                return [_mask(v) for v in obj]
            return obj

        return ctx.ok({'config': _mask(fw.config),
                       'path': getattr(fw, 'config_path', '')})

    @app.route('/api/system/config', methods=['POST'])
    @ctx.require_auth(role='super')
    def _config_save():
        """写回 config.yaml（点路径批量改）。

        body: ``{"values": {"log.level": "DEBUG", "api.port": 8000}}``
        或单条 ``{"path": "log.level", "value": "DEBUG"}``。

        文本级 patch，注释保留；路径不存在记入 missing。
        **端口 / 监听地址 / 数据库等改动需重启内核才生效** —— 接口只写盘，
        不擅自重启（重启由运维决定）。
        """
        from core.config import set_config_values
        body = request.get_json(silent=True) or {}
        values = dict(body.get('values') or {})
        if body.get('path'):
            values[str(body['path'])] = body.get('value')
        if not values:
            return ctx.fail('未提供任何配置项', 400)

        path = getattr(fw, 'config_path', '')
        if not path or not os.path.isfile(path):
            return ctx.fail('配置文件不可写（config_path 缺失）', 500)
        try:
            r = set_config_values(path, values)
        except Exception as e:
            logger.exception('写配置失败')
            return ctx.fail(f'写配置失败: {e}', 500)
        logger.info(f"[api] 配置已写回 {path}: {list(r['applied'].keys())}")
        return ctx.ok({**r, 'restart_required': True,
                       'path': path,
                       'hint': '已写入 config.yaml；端口 / 监听地址 / 数据库等项需重启内核生效'})

    @app.route('/api/system/extensions', methods=['GET'])
    @ctx.require_auth
    def _extensions():
        """官方扩展清单：名称、说明、开关（以 extensions.yaml 为准）。"""
        import tomllib
        root = os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
            'software', 'extensions')
        yaml_path = os.path.normpath(os.path.join(os.path.dirname(root), '..', 'extensions.yaml'))
        on = {}
        if os.path.isfile(yaml_path):
            try:
                with open(yaml_path, 'r', encoding='utf-8') as f:
                    on = ((yaml.safe_load(f) or {}).get('extensions') or {})
            except Exception:
                on = {}
        items = []
        if os.path.isdir(root):
            for name in sorted(os.listdir(root)):
                d = os.path.join(root, name)
                if name.startswith('_') or not os.path.isfile(os.path.join(d, 'main.py')):
                    continue
                meta = {}
                mp = os.path.join(d, 'manifest.toml')
                if os.path.isfile(mp):
                    try:
                        with open(mp, 'rb') as f:
                            meta = (tomllib.load(f) or {}).get('package') or {}
                    except Exception:
                        meta = {}
                blk = on.get(name) or {}
                enabled = blk.get('enabled')
                items.append({
                    'id': name,
                    'name': meta.get('name') or name,
                    'description': meta.get('description') or '',
                    'version': meta.get('version') or '',
                    'enabled': bool(enabled) if enabled is not None else True,
                })
        return ctx.ok({'extensions': items, 'count': len(items)})

    @app.route('/api/system/extensions/<name>', methods=['POST'])
    @ctx.require_auth
    def _extension_toggle(name):
        """开关某个官方扩展（写 extensions.yaml，重启后生效）。"""
        body = request.get_json(silent=True) or {}
        enabled = bool(body.get('enabled'))
        root = os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
            'software', 'extensions')
        if not os.path.isdir(os.path.join(root, name)):
            return ctx.fail(f'扩展不存在: {name}', 404)
        yaml_path = os.path.normpath(os.path.join(os.path.dirname(root), '..', 'extensions.yaml'))
        data = {}
        if os.path.isfile(yaml_path):
            try:
                with open(yaml_path, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f) or {}
            except Exception:
                data = {}
        blk = data.setdefault('extensions', {}).setdefault(name, {})
        if not isinstance(blk, dict):
            blk = {}
            data['extensions'][name] = blk
        blk['enabled'] = enabled
        try:
            with open(yaml_path, 'w', encoding='utf-8') as f:
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        except Exception as e:
            logger.exception('写 extensions.yaml 失败')
            return ctx.fail(f'写扩展配置失败: {e}', 500)
        logger.info(f"[api] 扩展 {name} 开关 → {enabled}（重启生效）")
        return ctx.ok({'id': name, 'enabled': enabled, 'restart_required': True})
