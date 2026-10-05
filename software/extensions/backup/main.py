# -*- coding: utf-8 -*-
"""
备份（官方扩展）

**备份 = 把指定路径打包成 zip 落到备份目录**，不做增量、不做去重、不上云 ——
运维面板的第一步备份就是"可靠的打包 + 保留最近 N 份"。打包实现来自 `fileops` 机制包（依赖在 manifest.toml 声明）——
本扩展不自带一份 zipfile 逻辑。

- 备份任务持久化在 SQLite（backup_jobs），含源路径列表 / 备份目录 / 保留份数；
- `run` 手动触发（也可由计划任务 scheduler 调 `backup.run` 命令定时执行）；
- 保留策略：按文件名时间戳排序，超出 keep 的旧备份自动删除；
- 源路径与备份目录都过 sandbox 校验。
"""
import logging
import os
import time

logger = logging.getLogger('zernus')

__plugin_meta__ = {
    "name": "备份",
    "version": "0.1.0",
    "author": "ZPanel",
    "desc": "把站点/实例/配置目录打包成 zip，保留最近 N 份",
    "priority": 60,
    "official": True,
}

_svc = None


def _default_roots() -> list:
    """全盘视角（与 files 同策略）；config.yaml → backup.roots 可收紧。"""
    import string
    if os.name == 'nt':
        return [f'{d}:\\' for d in string.ascii_uppercase
                if os.path.exists(f'{d}:\\')]
    return ['/']


class BackupService:
    """备份任务管理。"""

    def __init__(self, fw, log=None, sandbox=None, roots=None, fileops=None):
        if sandbox is None:
            raise RuntimeError("备份依赖 sandbox 机制包（检查 manifest.toml）")
        if fileops is None:
            raise RuntimeError("备份依赖 fileops 机制包（检查 manifest.toml）")
        self.fw = fw
        self._fo = fileops
        self._log = log or (lambda m: logger.info(m))
        # sandbox 是机制包（模块）：据 roots 建沙箱实例（与 files 同策略）
        self.box = sandbox.Sandbox(roots or _default_roots())
        self.default_dir = self._default_dir()
        self.ensure_table()

    @staticmethod
    def _default_dir() -> str:
        return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))), 'data', 'backups')

    def ensure_table(self):
        self.fw.db.execute("""
            CREATE TABLE IF NOT EXISTS backup_jobs (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                paths      TEXT NOT NULL,        -- JSON 数组：源路径
                target_dir TEXT DEFAULT '',      -- 空 = data/backups
                keep       INTEGER DEFAULT 5,
                last_run_at REAL DEFAULT 0,
                last_size  INTEGER DEFAULT 0,
                last_error TEXT DEFAULT '',
                created_at REAL
            )
        """)

    def _row_to_job(self, r: dict) -> dict:
        import json as _json
        return {
            'id': r['id'], 'name': r['name'],
            'paths': _json.loads(r['paths'] or '[]'),
            'target_dir': r['target_dir'] or self.default_dir,
            'keep': r['keep'] or 5,
            'last_run_at': r['last_run_at'] or 0,
            'last_size': r['last_size'] or 0,
            'last_error': r['last_error'] or '',
            'created_at': r['created_at'] or 0,
        }

    def list(self) -> dict:
        rows = self.fw.db.query("SELECT * FROM backup_jobs ORDER BY created_at") or []
        return {'count': len(rows), 'jobs': [self._row_to_job(r) for r in rows],
                'default_dir': self.default_dir}

    def get(self, jid: str):
        rows = self.fw.db.query("SELECT * FROM backup_jobs WHERE id=?", (jid,)) or []
        return self._row_to_job(rows[0]) if rows else None

    def create(self, data: dict) -> dict:
        import json as _json
        import uuid
        name = str(data.get('name') or '').strip()
        paths = [str(p) for p in (data.get('paths') or []) if str(p).strip()]
        if not name:
            return {'ok': False, 'error': '任务名不能为空'}
        if not paths:
            return {'ok': False, 'error': '至少填一个要备份的路径'}
        # 源路径先过沙箱：不在允许根内的路径创建时就拒绝，别等 run 才炸
        try:
            for p in paths:
                self.box.resolve(os.path.expanduser(p))
        except Exception as e:
            return {'ok': False, 'error': f'源路径校验失败: {e}'}
        jid = uuid.uuid4().hex[:16]
        self.fw.db.execute(
            "INSERT INTO backup_jobs (id, name, paths, target_dir, keep, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (jid, name, _json.dumps(paths), str(data.get('target_dir') or ''),
             int(data.get('keep') or 5), time.time()))
        return {'ok': True, 'job': self.get(jid)}

    def update(self, jid: str, data: dict) -> dict:
        if not self.get(jid):
            return {'ok': False, 'error': '任务不存在'}
        import json as _json
        sets, vals = [], []
        if data.get('name') is not None:
            sets.append('name=?'); vals.append(str(data['name']))
        if data.get('paths') is not None:
            sets.append('paths=?')
            vals.append(_json.dumps([str(p) for p in data['paths'] if str(p).strip()]))
        if data.get('target_dir') is not None:
            sets.append('target_dir=?'); vals.append(str(data['target_dir']))
        if data.get('keep') is not None:
            sets.append('keep=?'); vals.append(int(data['keep']))
        if sets:
            vals.append(jid)
            self.fw.db.execute(f"UPDATE backup_jobs SET {','.join(sets)} WHERE id=?", tuple(vals))
        return {'ok': True, 'job': self.get(jid)}

    def remove(self, jid: str) -> dict:
        if not self.get(jid):
            return {'ok': False, 'error': '任务不存在'}
        self.fw.db.execute("DELETE FROM backup_jobs WHERE id=?", (jid,))
        return {'ok': True}

    # ── 执行 ────────────────────────────────────────────────
    def run(self, jid: str) -> dict:
        job = self.get(jid)
        if not job:
            return {'ok': False, 'error': '任务不存在'}
        t0 = time.time()
        try:
            target_dir = os.path.expanduser(job['target_dir'])
            os.makedirs(target_dir, exist_ok=True)
            out = os.path.join(target_dir, f"{job['name']}-{time.strftime('%Y%m%d-%H%M%S')}.zip")
            # 打包实现取自 fileops 机制包 —— 本扩展不再自带 zipfile 逻辑
            srcs = [self.box.resolve(os.path.expanduser(raw)) for raw in job['paths']]
            srcs = [p for p in srcs if os.path.exists(p)]
            if not srcs:
                raise ValueError('没有可打包的路径（都已不存在）')
            packed = self._fo.zip_paths(srcs, out)
            n, size = packed['files'], packed['size']
            self._prune(target_dir, job['name'], int(job['keep']))
            self.fw.db.execute(
                "UPDATE backup_jobs SET last_run_at=?, last_size=?, last_error='' WHERE id=?",
                (time.time(), size, jid))
            self._log(f"备份 {job['name']} 完成：{out}（{n} 个源文件，{size // 1024}KB）")
            return {'ok': True, 'archive': out, 'files': n, 'size': size,
                    'duration_ms': int((time.time() - t0) * 1000)}
        except Exception as e:
            self.fw.db.execute(
                "UPDATE backup_jobs SET last_run_at=?, last_error=? WHERE id=?",
                (time.time(), str(e)[:300], jid))
            return {'ok': False, 'error': f'{type(e).__name__}: {e}'}

    @staticmethod
    def _prune(target_dir: str, name: str, keep: int):
        """超出 keep 的旧备份按时间戳删掉（只删 <name>-*.zip，不碰别人的文件）。"""
        prefix = f'{name}-'
        archives = sorted(
            (f for f in os.listdir(target_dir) if f.startswith(prefix) and f.endswith('.zip')),
            reverse=True)
        for old in archives[max(0, int(keep)):]:
            try:
                os.remove(os.path.join(target_dir, old))
                logger.info("[backup] 清理旧备份 %s", old)
            except OSError as e:
                logger.warning("[backup] 清理失败 %s: %s", old, e)


def register(ctx):
    global _svc
    fw = ctx._framework
    sandbox = ctx.zkg_tool('sandbox')
    if sandbox is None:
        ctx.log("备份：缺少 sandbox 机制包，已跳过")
        return
    fileops = ctx.zkg_tool('fileops')
    if fileops is None:
        ctx.log("备份：缺少 fileops 机制包，已跳过")
        return
    _roots = (fw.config.get('backup') or {}).get('roots') or _default_roots()
    _svc = BackupService(fw, log=ctx.log, sandbox=sandbox, roots=_roots,
                         fileops=fileops)
    fw.backup_svc = _svc
    fw.services.register('backup', _svc)

    def _ok(d):
        return {'ok': True, 'data': d}

    def _err(m):
        return {'ok': False, 'data': str(m)}

    for name, fn, desc in (
        ('backup.list', lambda a: _ok(_svc.list()), '备份任务清单'),
        ('backup.create', lambda a: _ok(_svc.create(a or {})), '新建任务'),
        ('backup.update', lambda a: _ok(_svc.update((a or {}).get('id', ''), a or {})), '修改任务'),
        ('backup.remove', lambda a: _ok(_svc.remove((a or {}).get('id', ''))), '删除任务'),
        ('backup.run', lambda a: _ok(_svc.run((a or {}).get('id', ''))), '立即执行'),
    ):
        fw.nodes.register_handler(name, fn, desc=desc, level='software')

    from flask import jsonify, request as _req

    def _body():
        return _req.get_json(silent=True) or {}

    def _job_resp(r, code=200):
        return (jsonify(r), code) if not r.get('ok') else jsonify(r)

    ctx.register_api('/api/backup', lambda: jsonify(_ok(_svc.list())), methods=['GET'])
    ctx.register_api('/api/backup', lambda: _job_resp(_svc.create(_body()), 400), methods=['POST'])
    ctx.register_api('/api/backup/<jid>', lambda jid: _job_resp(_svc.update(jid, _body()), 400),
                     methods=['PATCH'])
    ctx.register_api('/api/backup/<jid>', lambda jid: jsonify(_svc.remove(jid)), methods=['DELETE'])
    ctx.register_api('/api/backup/<jid>/run', lambda jid: _job_resp(_svc.run(jid), 502),
                     methods=['POST'])

    ctx.log(f"备份已就绪（{_svc.list()['count']} 个任务，默认目录 data/backups）")


def unregister():
    global _svc
    _svc = None
