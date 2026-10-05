# -*- coding: utf-8 -*-
"""
计划任务（官方扩展）

**管系统的调度器，不自己发明调度器**：
- POSIX  → `crontab`（只维护带托管标记的行，别人的配置不碰）
- Windows → `schtasks`（Windows 没有 cron，就用它的任务计划程序）

表达式解析/下次触发时间/人类可读描述全部来自 `cron` 机制包，
系统命令执行来自 `procs` 机制包 —— 本扩展只做「任务清单 + 下发 + 校验」。

诚实边界：cron 的表达力比 schtasks 强，覆盖不了的周期**明确标 unsupported 且不下发**，
绝不写一个"差不多"的计划进去（那会静默变成错的调度）。
"""
import json
import logging
import os
import uuid

logger = logging.getLogger('zernus')

__plugin_meta__ = {
    "name": "计划任务",
    "version": "0.1.0",
    "author": "ZPanel",
    "desc": "管理系统自己的计划任务（crontab / schtasks），表达式由 cron 机制包校验",
    "priority": 28,
    "official": True,
}

_subsystem = None

_DDL = """
CREATE TABLE IF NOT EXISTS sched_tasks (
    id         VARCHAR(64) NOT NULL PRIMARY KEY,
    name       VARCHAR(120) DEFAULT '',
    expr       VARCHAR(120) DEFAULT '',
    command    VARCHAR(1000) DEFAULT '',
    enabled    INTEGER      DEFAULT 1,
    remark     VARCHAR(500) DEFAULT '',
    status     VARCHAR(20)  DEFAULT 'pending',
    note       VARCHAR(500) DEFAULT '',
    created_at VARCHAR(32)  DEFAULT '',
    updated_at VARCHAR(32)  DEFAULT ''
)
"""


class SchedulerSubsystem:
    def __init__(self, fw, log=None):
        self.fw = fw
        self._log = log or (lambda m: logger.info(m))
        self._lock = None
        import threading
        self._lock = threading.RLock()

    # ── 持久化 ────────────────────────────────────────────
    def ensure_table(self):
        try:
            self.fw.db.execute(_DDL)
            return True
        except Exception as e:
            logger.error("[scheduler] 建表失败: %s", e)
            return False

    def _load(self, tid):
        rows = self.fw.db.query("SELECT * FROM sched_tasks WHERE id=?", (tid,)) or []
        return rows[0] if rows else None

    def list(self):
        rows = self.fw.db.query("SELECT * FROM sched_tasks ORDER BY created_at") or []
        out = []
        for r in rows:
            d = dict(r)
            d['enabled'] = bool(d.get('enabled'))
            d['next_runs'] = self.preview(d.get('expr', ''))
            out.append(d)
        return out

    def count(self):
        rows = self.fw.db.query(
            "SELECT COUNT(*) n, SUM(enabled) on_n FROM sched_tasks") or []
        r = rows[0] if rows else {}
        return {'total': int(r.get('n') or 0), 'enabled': int(r.get('on_n') or 0)}

    # ── 表达式校验（cron 机制包）───────────────────────────
    @staticmethod
    def validate(expr: str) -> dict:
        import zkg
        cron = zkg.tool('cron')
        if cron is None:
            return {'ok': False, 'error': 'cron 机制包未加载'}
        e = str(expr or '').strip()
        if not e:
            return {'ok': False, 'error': '表达式为空'}
        try:
            runs = cron.next_runs(e, 5)
        except Exception as ex:
            return {'ok': False, 'error': str(ex)}
        return {'ok': True, 'expr': e, 'describe': cron.describe(e),
                'next_runs': [r.strftime('%Y-%m-%d %H:%M') for r in runs]}

    @staticmethod
    def preview(expr: str, n: int = 3):
        try:
            v = SchedulerSubsystem.validate(expr)
            return v.get('next_runs', [])[:n] if v.get('ok') else []
        except Exception:
            return []

    # ── CRUD ──────────────────────────────────────────────
    def create(self, spec: dict) -> dict:
        from software.extensions.scheduler.backends import detect_backend
        name = str(spec.get('name') or '').strip()
        expr = str(spec.get('expr') or '').strip()
        command = str(spec.get('command') or '').strip()
        if not name:
            return {'ok': False, 'error': '任务名不能为空'}
        if not command:
            return {'ok': False, 'error': '要执行的命令不能为空'}
        v = self.validate(expr)
        if not v.get('ok'):
            return {'ok': False, 'error': f"表达式不可用: {v.get('error')}"}

        tid = uuid.uuid4().hex[:16]
        now = _now()
        rec = {'id': tid, 'name': name, 'expr': expr, 'command': command,
               'enabled': 1 if spec.get('enabled', True) else 0,
               'remark': str(spec.get('remark') or ''),
               'status': 'pending', 'note': '',
               'created_at': now, 'updated_at': now}
        self._upsert(rec)
        be = detect_backend()
        return {'ok': True, 'task': self._load(tid), 'backend': be}

    def update(self, tid: str, data: dict) -> dict:
        rec = self._load(tid)
        if rec is None:
            return {'ok': False, 'error': '任务不存在'}
        for k in ('name', 'expr', 'command', 'remark'):
            if k in data and data[k] is not None:
                rec[k] = str(data[k]).strip()
        if 'enabled' in data:
            rec['enabled'] = 1 if data['enabled'] else 0
        v = self.validate(rec['expr'])
        if not v.get('ok'):
            return {'ok': False, 'error': f"表达式不可用: {v.get('error')}"}
        rec['updated_at'] = _now()
        self._upsert(rec)
        return {'ok': True, 'task': self._load(tid)}

    def remove(self, tid: str) -> dict:
        rec = self._load(tid)
        if rec is None:
            return {'ok': False, 'error': '任务不存在'}
        removed = self._system_remove(dict(rec))
        self.fw.db.execute("DELETE FROM sched_tasks WHERE id=?", (tid,))
        return {'ok': True, 'removed': tid, 'system': removed}

    def _upsert(self, rec: dict):
        self.fw.db.execute(
            "DELETE FROM sched_tasks WHERE id=?", (rec['id'],))
        self.fw.db.execute(
            "INSERT INTO sched_tasks (id,name,expr,command,enabled,remark,"
            "status,note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rec['id'], rec['name'], rec['expr'], rec['command'],
             int(rec['enabled']), rec['remark'], rec.get('status', 'pending'),
             rec.get('note', ''), rec['created_at'], rec['updated_at']))

    # ── 下发 ──────────────────────────────────────────────
    def backends(self) -> dict:
        from software.extensions.scheduler.backends import detect_backend
        be = detect_backend()
        be['supported_kinds'] = ['*/N * * * *', 'M * * * *', 'M H * * *',
                                 'M H * * D', 'M H D * *']
        return be

    def apply(self) -> dict:
        """把清单下发到系统调度器；逐个记录结果，不中断。"""
        from software.extensions.scheduler.backends import (
            detect_backend, apply_schtasks, apply_crontab, query_schtasks)
        be = detect_backend()
        backend = be.get('backend')
        tasks = [dict(r) for r in (self.fw.db.query(
            "SELECT * FROM sched_tasks") or [])]
        results = []
        if not backend:
            for t in tasks:
                self._set_status(t['id'], 'unsupported', '本机没有可用的系统调度器')
                results.append({'id': t['id'], 'ok': False, 'status': 'unsupported'})
            return {'ok': False, 'backend': None, 'results': results,
                    'note': '未找到 crontab / schtasks，任务仅记录在面板内'}

        for t in tasks:
            t['enabled'] = bool(t['enabled'])
            try:
                if not t['enabled']:
                    r = self._system_remove(t)
                elif backend == 'schtasks':
                    r = apply_schtasks(t)
                else:
                    r = apply_crontab(t, tasks)
            except Exception as e:
                r = {'ok': False, 'status': 'error', 'output': str(e)}
            self._set_status(t['id'], r.get('status', 'error'), r.get('output', '')[:300])
            results.append({'id': t['id'], 'name': t['name'], **r})

        drift = []
        if backend == 'schtasks':
            sysnames = {n.lower() for n in query_schtasks()}
            for t in tasks:
                if t['enabled'] and f"zpanel-{t['id']}".lower() not in sysnames:
                    drift.append(t['id'])

        ok = all(r.get('ok') for r in results)
        return {'ok': ok, 'backend': backend, 'count': len(results),
                'results': results, 'missing_in_system': drift}

    def run_now(self, tid: str) -> dict:
        """立即执行一次（不等待调度）—— 走 procs，超时整树终止。"""
        rec = self._load(tid)
        if rec is None:
            return {'ok': False, 'error': '任务不存在'}
        import zkg
        procs = zkg.tool('procs')
        if procs is None:
            return {'ok': False, 'error': 'procs 机制包未加载'}
        r = procs.run(rec['command'], shell=True, timeout=120)
        return {'ok': r.ok, 'code': r.returncode, 'stdout': r.stdout,
                'stderr': r.stderr, 'duration_ms': int(r.duration * 1000)}

    def _system_remove(self, task: dict) -> dict:
        from software.extensions.scheduler.backends import (
            detect_backend, remove_schtasks, remove_crontab)
        backend = detect_backend().get('backend')
        try:
            if backend == 'schtasks':
                return remove_schtasks(task)
            if backend == 'crontab':
                all_tasks = [dict(r) for r in (self.fw.db.query(
                    "SELECT * FROM sched_tasks") or [])]
                for t in all_tasks:
                    t['enabled'] = bool(t['enabled'])
                return remove_crontab(task, all_tasks)
        except Exception as e:
            return {'ok': False, 'status': 'error', 'output': str(e)}
        return {'ok': True, 'status': 'no-backend'}

    def _set_status(self, tid: str, status: str, note: str = ''):
        try:
            self.fw.db.execute(
                "UPDATE sched_tasks SET status=?, note=? WHERE id=?",
                (status, str(note)[:300], tid))
        except Exception:
            pass


def _now() -> str:
    import time
    return time.strftime('%Y-%m-%d %H:%M:%S')


def register(ctx):
    global _subsystem
    fw = ctx._framework
    cfg = fw.config.get('scheduler') or {}
    if cfg.get('enabled', True) is False:
        ctx.log("计划任务已禁用 (scheduler.enabled: false)")
        return

    # 机制包依赖由 manifest.toml 声明；取不到就是依赖图坏了
    if ctx.zkg_tool('cron') is None or ctx.zkg_tool('procs') is None:
        raise RuntimeError(
            "计划任务依赖 cron / procs 机制包，但未加载 —— "
            "检查 software/extensions/scheduler/manifest.toml 的 dependencies")

    _subsystem = SchedulerSubsystem(fw, log=ctx.log)
    # 注意：fw.scheduler 是内核的**只读属性**（任务队列调度器），不能赋值；
    # 这里沿用和 terminal_svc 一致的命名挂到框架上。
    fw.scheduler_svc = _subsystem
    # 注意：services['scheduler'] 是内核 TaskScheduler 的预留键（core/runtime/lifecycle.py
    # 在扩展加载后写入），本扩展管理的是系统级 crontab/schtasks，属于「管系统环境」，
    # 不应覆盖内核任务队列。改用 scheduler_ext 键，避免 framework.scheduler._jobs /
    # add_plugin_task / remove_plugin_tasks 被顶掉导致核心自检与插件 cron 失效。
    fw.services.register('scheduler_ext', _subsystem)
    _subsystem.ensure_table()

    # ── 数据源：随心跳上报（中心机能看到各节点有几个任务、下次何时跑）──
    fw.nodes.register_provider(
        'scheduler',
        lambda: {'count': _subsystem.count(),
                 'backend': _subsystem.backends().get('backend'),
                 'next': [{'name': t['name'], 'next': (t.get('next_runs') or ['—'])[0]}
                          for t in _subsystem.list()[:5]]},
        desc='计划任务概览', level='software')

    def _ok(d):
        return {'ok': True, 'data': d}

    def _err(m):
        return {'ok': False, 'data': str(m)}

    def _guard(fn, args):
        try:
            return _ok(fn(args or {}))
        except Exception as e:
            return _err(f'{type(e).__name__}: {e}')

    def _h_list(a):
        r = _subsystem.list()
        return _ok({'tasks': r, 'count': _subsystem.count()})

    def _h_create(a):
        r = _subsystem.create(a)
        return _ok(r['task']) if r.get('ok') else _err(r.get('error'))

    def _h_update(a):
        r = _subsystem.update(a.get('id', ''), a)
        return _ok(r['task']) if r.get('ok') else _err(r.get('error'))

    def _h_remove(a):
        r = _subsystem.remove(a.get('id', ''))
        return _ok({'removed': True, 'system': r.get('system')}) if r.get('ok') \
            else _err(r.get('error'))

    def _h_apply(a):
        return _ok(_subsystem.apply())

    def _h_validate(a):
        return _ok(_subsystem.validate((a or {}).get('expr', '')))

    def _h_backends(a):
        return _ok(_subsystem.backends())

    def _h_run(a):
        r = _subsystem.run_now((a or {}).get('id', ''))
        return _ok(r) if r.get('ok') or 'stdout' in r else _err(r.get('error'))

    for name, fn, desc in (
        ('scheduler.list', _h_list, '任务清单'),
        ('scheduler.create', _h_create, '创建任务'),
        ('scheduler.update', _h_update, '更新任务'),
        ('scheduler.remove', _h_remove, '删除任务'),
        ('scheduler.apply', _h_apply, '下发到系统调度器'),
        ('scheduler.validate', _h_validate, '校验 cron 表达式'),
        ('scheduler.backends', _h_backends, '系统调度器能力'),
        ('scheduler.run', _h_run, '立即执行一次'),
    ):
        fw.nodes.register_handler(name, fn, desc=desc, level='software')

    # ── HTTP API ──
    from flask import jsonify, request as _req

    def _body():
        return _req.get_json(silent=True) or {}

    def _api_list():
        return jsonify({'ok': True, 'tasks': _subsystem.list(),
                        'count': _subsystem.count(),
                        'backend': _subsystem.backends()})

    def _api_create():
        r = _subsystem.create(_body())
        return (jsonify({'ok': True, 'task': r['task']}) if r.get('ok')
                else (jsonify({'ok': False, 'error': r.get('error')}), 400))

    def _api_update(tid):
        r = _subsystem.update(tid, _body())
        return (jsonify({'ok': True, 'task': r['task']}) if r.get('ok')
                else (jsonify({'ok': False, 'error': r.get('error')}), 400))

    def _api_remove(tid):
        r = _subsystem.remove(tid)
        return (jsonify({'ok': True, **r}) if r.get('ok')
                else (jsonify({'ok': False, 'error': r.get('error')}), 404))

    def _api_apply():
        return jsonify({'ok': True, **_subsystem.apply()})

    def _api_validate():
        return jsonify({'ok': True, **_subsystem.validate(_req.args.get('expr') or '')})

    def _api_backends():
        return jsonify({'ok': True, **_subsystem.backends()})

    def _api_run(tid):
        return jsonify({'ok': True, **_subsystem.run_now(tid)})

    ctx.register_api('/api/scheduler', _api_list, methods=['GET'])
    ctx.register_api('/api/scheduler', _api_create, methods=['POST'])
    ctx.register_api('/api/scheduler/validate', _api_validate, methods=['GET'])
    ctx.register_api('/api/scheduler/backends', _api_backends, methods=['GET'])
    ctx.register_api('/api/scheduler/apply', _api_apply, methods=['POST'])
    ctx.register_api('/api/scheduler/<tid>', _api_update, methods=['PATCH'])
    ctx.register_api('/api/scheduler/<tid>', _api_remove, methods=['DELETE'])
    ctx.register_api('/api/scheduler/<tid>/run', _api_run, methods=['POST'])

    be = _subsystem.backends()
    ctx.log(f"计划任务已就绪：系统调度器={be.get('backend') or '不可用'}"
            f"（{be.get('platform')}），已有 {_subsystem.count()['total']} 个任务")
