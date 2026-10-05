# -*- coding: utf-8 -*-
"""
实例子系统（Runtime Subsystem）—— 实例注册表与生命周期编排

职责：
- **持久化**：实例配置落 SQLite（`instances` 表），重启后自动恢复；
- **编排**：创建 / 删除 / 启停 / 重启 / 批量自启；
- **输出总线**：把实例输出同时喂给多个订阅者（面板 WebSocket、日志落盘、内核流帧）；
- **暴露给内核**：实例清单与操作以「数据源 + 命令」的形式注册进 `fw.nodes`，
  于是**远程节点无需额外代码**就能管理自己机器上的实例
  （hub 侧 `send_cmd('node-1', 'runtime.start', {...})` 即可）。

设计边界：本模块只管「进程」，不关心它是 PHP / Node / Java ——
具体运行时的探测与命令生成在 `adapters/`，通过 `kind` 分发。
"""
from __future__ import annotations

import json
import logging
import threading
import time

from software.extensions.runtime.instance import Instance, Status

logger = logging.getLogger('zernus')

_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS instances (
    id             VARCHAR(64)  NOT NULL PRIMARY KEY,
    name           VARCHAR(100) NOT NULL,
    kind           VARCHAR(32)  DEFAULT 'generic',
    cwd            VARCHAR(500) DEFAULT '',
    start_command  TEXT         DEFAULT '',
    stop_command   VARCHAR(255) DEFAULT '',
    stop_timeout   INTEGER      DEFAULT 20,
    env            TEXT         DEFAULT '{}',
    port           INTEGER      DEFAULT 0,
    auto_start     INTEGER      DEFAULT 0,
    auto_restart   INTEGER      DEFAULT 0,
    max_restarts   INTEGER      DEFAULT -1,
    runtime        VARCHAR(64)  DEFAULT '',
    tags           VARCHAR(255) DEFAULT '',
    created_at     VARCHAR(32)  DEFAULT NULL,
    updated_at     VARCHAR(32)  DEFAULT NULL
)
"""


class RuntimeSubsystem:
    """实例注册表 + 生命周期编排。"""

    def __init__(self, fw, log=None):
        self.fw = fw
        self._log = log or (lambda m: logger.info(m))
        self._lock = threading.RLock()
        self._instances = {}          # id -> Instance
        self._ready = False
        self._output_bridge = None    # 全局输出桥：fn(instance_id, stream, line)，
                                      # 由入口层注入（接到内核实时推送）

    def set_output_bridge(self, cb) -> None:
        """注册全局输出桥：之后**所有**实例（含新建/恢复的）的每一行输出都会回调 cb。

        之所以放在 subsystem 而不是逐个实例 subscribe：新建实例、从库恢复的实例
        都会漏掉手动注册 —— 这里保证一处注册、全体生效。
        """
        self._output_bridge = cb
        with self._lock:
            for inst in self._instances.values():
                inst.subscribe(cb)

    def _on_new(self, inst: Instance) -> None:
        """每个新实例都要挂上输出桥（_emit 异常安全，桥出错不影响采集）。"""
        if self._output_bridge:
            inst.subscribe(self._output_bridge)

    # ══════════════════════════════════════════════════════
    # 持久化
    # ══════════════════════════════════════════════════════
    def ensure_table(self) -> bool:
        if self._ready:
            return True
        try:
            ddl = _DDL_SQLITE
            if getattr(self.fw.db, 'db_type', 'sqlite') == 'mysql':
                ddl = ddl.rstrip() + ' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'
            self.fw.db.execute(ddl)
            self._ready = True
            return True
        except Exception as e:
            if 'exist' in str(e).lower():
                self._ready = True
                return True
            logger.error("[runtime] 建表失败: %s", e)
            return False

    @staticmethod
    def _row_to_cfg(row: dict) -> dict:
        cfg = dict(row)
        try:
            cfg['env'] = json.loads(cfg.get('env') or '{}')
        except Exception:
            cfg['env'] = {}
        cfg['tags'] = [t for t in str(cfg.get('tags') or '').split(',') if t]
        cfg['auto_start'] = bool(cfg.get('auto_start'))
        cfg['auto_restart'] = bool(cfg.get('auto_restart'))
        return cfg

    def _save(self, inst: Instance) -> None:
        cfg = inst.to_config()
        try:
            exists = self.fw.db.query_one("SELECT id FROM instances WHERE id=?", (cfg['id'],))
            fields = ('name', 'kind', 'cwd', 'start_command', 'stop_command',
                      'stop_timeout', 'env', 'port', 'auto_start', 'auto_restart',
                      'max_restarts', 'runtime', 'tags', 'updated_at')
            vals = (cfg['name'], cfg['kind'], cfg['cwd'], cfg['start_command'],
                    cfg['stop_command'], cfg['stop_timeout'],
                    json.dumps(cfg['env'], ensure_ascii=False), cfg['port'],
                    int(cfg['auto_start']), int(cfg['auto_restart']),
                    cfg['max_restarts'], cfg['runtime'],
                    ','.join(cfg['tags'] or []), cfg['updated_at'])
            if exists:
                sets = ', '.join(f"{f}=?" for f in fields)
                self.fw.db.execute(f"UPDATE instances SET {sets} WHERE id=?",
                                   vals + (cfg['id'],))
            else:
                cols = ('id',) + fields + ('created_at',)
                ph = ', '.join('?' for _ in cols)
                self.fw.db.execute(
                    f"INSERT INTO instances ({', '.join(cols)}) VALUES ({ph})",
                    (cfg['id'],) + vals + (cfg['created_at'],))
        except Exception as e:
            logger.error("[runtime] 保存实例 %s 失败: %s", cfg['id'], e)

    def load_all(self) -> int:
        """启动时把库里的实例全部装进内存（不自动拉起）。"""
        self.ensure_table()
        try:
            rows = self.fw.db.query("SELECT * FROM instances ORDER BY created_at") or []
        except Exception as e:
            logger.error("[runtime] 读取实例失败: %s", e)
            return 0
        with self._lock:
            for row in rows:
                cfg = self._row_to_cfg(row)
                inst = Instance(cfg, log=self._log)
                self._on_new(inst)
                self._instances[cfg['id']] = inst
        self._log(f"已加载 {len(rows)} 个实例配置")
        return len(rows)

    # ══════════════════════════════════════════════════════
    # 查询
    # ══════════════════════════════════════════════════════
    def list(self) -> list:
        with self._lock:
            return [i.snapshot() for i in self._instances.values()]

    def get(self, iid: str) -> Instance:
        with self._lock:
            return self._instances.get(str(iid))

    def count(self) -> dict:
        with self._lock:
            items = list(self._instances.values())
        return {
            'total': len(items),
            'running': sum(1 for i in items if i.status == Status.RUNNING),
            'stopped': sum(1 for i in items if i.status == Status.STOP),
            'transitional': sum(1 for i in items
                                if i.status in (Status.STARTING, Status.STOPPING, Status.BUSY)),
        }

    # ══════════════════════════════════════════════════════
    # 增删
    # ══════════════════════════════════════════════════════
    def create(self, spec: dict) -> dict:
        spec = dict(spec or {})
        if not str(spec.get('name') or '').strip():
            return {'ok': False, 'error': '实例名不能为空'}
        if not str(spec.get('start_command') or '').strip():
            return {'ok': False, 'error': '启动命令不能为空'}
        self.ensure_table()
        inst = Instance(spec, log=self._log)
        self._on_new(inst)
        with self._lock:
            if inst.id in self._instances:
                inst.id = inst.id + '-' + str(int(time.time()))[-4:]
            self._instances[inst.id] = inst
        self._save(inst)
        self._log(f"已创建实例 {inst.name}（{inst.kind}，id={inst.id}）")
        return {'ok': True, 'instance': inst.snapshot()}

    def update(self, iid: str, data: dict) -> dict:
        inst = self.get(iid)
        if inst is None:
            return {'ok': False, 'error': f'实例 {iid} 不存在'}
        cfg = inst.update_config(data)
        self._save(inst)
        return {'ok': True, 'instance': inst.snapshot()}

    def remove(self, iid: str) -> dict:
        inst = self.get(iid)
        if inst is None:
            return {'ok': False, 'error': f'实例 {iid} 不存在'}
        if inst.is_running():
            inst.stop(force=True)
        with self._lock:
            self._instances.pop(iid, None)
        try:
            self.fw.db.execute("DELETE FROM instances WHERE id=?", (iid,))
        except Exception as e:
            logger.error("[runtime] 删除实例记录失败: %s", e)
        self._log(f"已删除实例 {inst.name}")
        return {'ok': True}

    # ══════════════════════════════════════════════════════
    # 操作
    # ══════════════════════════════════════════════════════
    def _act(self, iid: str, action: str, **kw) -> dict:
        inst = self.get(iid)
        if inst is None:
            return {'ok': False, 'error': f'实例 {iid} 不存在'}
        fn = getattr(inst, action, None)
        if fn is None:
            return {'ok': False, 'error': f'未知操作 {action}'}
        res = fn(**kw)
        # 状态变化后落库（自启计数等运行态不落库，配置才落）
        return res

    def start(self, iid: str) -> dict:
        return self._act(iid, 'start')

    def stop(self, iid: str, force: bool = False) -> dict:
        return self._act(iid, 'stop', force=force)

    def restart(self, iid: str) -> dict:
        return self._act(iid, 'restart')

    def write_stdin(self, iid: str, data: str) -> dict:
        inst = self.get(iid)
        if inst is None:
            return {'ok': False, 'error': f'实例 {iid} 不存在'}
        return inst.write_stdin(data)

    def logs(self, iid: str, limit: int = 200) -> dict:
        inst = self.get(iid)
        if inst is None:
            return {'ok': False, 'error': f'实例 {iid} 不存在'}
        return {'ok': True, 'lines': inst.history(limit)}

    def auto_start_all(self) -> int:
        """按 auto_start 逐个拉起，**每个之间错开 3 秒**（避免启动风暴）。"""
        targets = [i for i in self.list() if i.get('auto_start')]
        if not targets:
            return 0
        self._log(f"自启 {len(targets)} 个实例（逐个错峰拉起）")
        for snap in targets:
            try:
                self.start(snap['id'])
            except Exception as e:
                logger.warning("[runtime] 自启实例 %s 失败: %s", snap['name'], e)
            time.sleep(3)
        return len(targets)

    def stop_all(self) -> int:
        with self._lock:
            items = list(self._instances.values())
        n = 0
        for inst in items:
            if inst.is_running():
                try:
                    inst.stop(force=True)
                    n += 1
                except Exception as e:
                    logger.warning("[runtime] 停止实例 %s 失败: %s", inst.name, e)
        return n

    # ══════════════════════════════════════════════════════
    # 输出订阅（供面板流式查看 / 内核流帧上行）
    # ══════════════════════════════════════════════════════
    def subscribe(self, iid: str, cb) -> dict:
        inst = self.get(iid)
        if inst is None:
            return {'ok': False, 'error': f'实例 {iid} 不存在'}
        inst.subscribe(cb)
        return {'ok': True, 'id': iid}

    def unsubscribe(self, iid: str, cb) -> dict:
        inst = self.get(iid)
        if inst is None:
            return {'ok': False, 'error': f'实例 {iid} 不存在'}
        inst.unsubscribe(cb)
        return {'ok': True, 'id': iid}
