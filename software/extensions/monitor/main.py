# -*- coding: utf-8 -*-
"""
系统监控（官方扩展）

面板第一眼就该回答的问题：「这台机器现在怎么样」。
数据全部来自 `sysres` 机制包（psutil 优先、stdlib 兜底），
本扩展只做三件事：暴露接口、注册数据源（多机时随心跳上报）、如实转述拿不到的字段。

**不编数据**：机制包拿不到的字段就是 None，前端据此灰掉，而不是显示 0 假装正常。

历史趋势（对标 1Panel BTTask/systemTask 的「采集即清理」模式）：
后台线程按 `monitor.history_interval`（默认 60s）采样进 SQLite 单表，
每拍顺手删掉超过 `monitor.history_days`（默认 30 天）的旧行——
不需要独立的清理任务；查询命令 `monitor.history` 按时间范围降采样返回。
"""
import logging
import threading
import time

logger = logging.getLogger('zernus')

__plugin_meta__ = {
    "name": "系统监控",
    "version": "0.2.0",
    "author": "ZPanel",
    "desc": "CPU / 内存 / 交换 / 磁盘 / 网络 / 负载 / 进程 TOP / 历史趋势",
    "priority": 30,
    "official": True,
}

_service = None
_recorder = None


class MonitorService:
    """主机资源读取（薄封装：采样节流 + 字段裁剪）。"""

    def __init__(self, sysres, log=None, min_interval: float = 1.0):
        if sysres is None:
            raise RuntimeError(
                "系统监控依赖 sysres 机制包，但未被加载 —— "
                "检查 software/extensions/monitor/manifest.toml 的 dependencies")
        self.sysres = sysres
        self._log = log or (lambda m: logger.info(m))
        self.min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._last = None
        self._cache = None
        # top() 的全进程表要扫两遍 + 强制 sample 睡 150ms，比 snapshot 贵一个量级，
        # 单独节流（默认 2s），监控页 2s 轮询不再反复打 psutil。
        self._top_lock = threading.Lock()
        self._top_cache = None
        self._top_at = 0.0

    def snapshot(self, force: bool = False) -> dict:
        """取一次快照；min_interval 内的重复请求直接复用（前端多卡片并发时不打爆 psutil）。"""
        with self._lock:
            now = time.time()
            if (not force and self._cache is not None
                    and self._last is not None
                    and now - self._last < self.min_interval):
                out = dict(self._cache)
                out['cached'] = True
                return out
            data = self.sysres.snapshot(with_disks=True, with_net=True, top=0)
        data['sampled_at'] = int(now)
        data['cached'] = False
        with self._lock:
            self._cache, self._last = data, now
        return data

    def top(self, n: int = 10, sort: str = 'cpu') -> dict:
        n = max(1, min(int(n or 10), 100))
        with self._top_lock:
            now = time.time()
            ttl = max(self.min_interval, 2.0)
            if (self._top_cache is not None and now - self._top_at < ttl
                    and self._top_cache.get('sort') == sort):
                out = dict(self._top_cache)
                out['cached'] = True
                return out
            out = {'processes': self.sysres.top_processes(n, sort),
                   'sort': sort, 'available': self.sysres.backend() == 'psutil'}
            self._top_cache, self._top_at = out, now
            return out

    def info(self) -> dict:
        return {**self.sysres.info(), 'min_interval': self.min_interval}


class HistoryRecorder:
    """监控历史：定时采样 → SQLite 单表 → 采集即清理（无需后台清理任务）。"""

    def __init__(self, fw, service, interval: int = 60, days: int = 30,
                 table: str = 'monitor_history'):
        self.fw = fw
        self.svc = service
        self.interval = max(5, int(interval))
        self.days = max(1, int(days))
        self.table = table
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self.fw.db.execute(
            f"CREATE TABLE IF NOT EXISTS {self.table} ("
            "ts INTEGER PRIMARY KEY, cpu REAL, mem REAL, load1 REAL, disk REAL)")
        self._thread = threading.Thread(target=self._loop,
                                        name='monitor-history', daemon=True)
        self._thread.start()
        logger.info("[monitor] 历史采样已启动：每 %ds 一拍，保留 %d 天", self.interval, self.days)

    def stop(self):
        self._stop.set()

    def _loop(self):
        # 首拍等一个完整 interval：避开启动期争用，也让「间隔」语义稳定
        while not self._stop.wait(self.interval):
            try:
                s = self.svc.snapshot()
                ts = int(time.time())
                load = s.get('load') or []
                disks = s.get('disks') or []
                disk = max((d.get('percent') for d in disks
                            if d.get('percent') is not None), default=None)
                self.fw.db.execute(
                    f"INSERT OR REPLACE INTO {self.table} (ts, cpu, mem, load1, disk) "
                    "VALUES (?,?,?,?,?)",
                    (ts, (s.get('cpu') or {}).get('percent'),
                     (s.get('memory') or {}).get('percent'),
                     load[0] if load else None, disk))
                self.fw.db.execute(
                    f"DELETE FROM {self.table} WHERE ts < ?",
                    (ts - self.days * 86400,))
            except Exception as e:
                logger.warning("[monitor] 历史采样失败: %s", e)

    def query(self, hours: float = 24) -> dict:
        """按时间范围返回降采样后的趋势点（≤400 桶，前端直接画线）。"""
        hours = max(0.25, min(float(hours or 24), self.days * 24))
        cutoff = int(time.time() - hours * 3600)
        rows = self.fw.db.query(
            f"SELECT ts, cpu, mem, load1, disk FROM {self.table} "
            "WHERE ts >= ? ORDER BY ts", (cutoff,))
        bucket = max(self.interval, int(hours * 3600 / 400))
        points, cur, cur_key = [], [], None
        for r in rows:
            key = r['ts'] // bucket
            if key != cur_key and cur:
                points.append(_avg_point(cur))
                cur = []
            cur_key = key
            cur.append(r)
        if cur:
            points.append(_avg_point(cur))
        return {'points': points, 'interval': self.interval,
                'days': self.days, 'bucket': bucket}


def _avg_point(rows: list) -> dict:
    def avg(field):
        vals = [r[field] for r in rows if r[field] is not None]
        return round(sum(vals) / len(vals), 2) if vals else None
    return {'t': rows[-1]['ts'], 'cpu': avg('cpu'), 'mem': avg('mem'),
            'load': avg('load1'), 'disk': avg('disk')}


def register(ctx):
    global _service, _recorder
    fw = ctx._framework
    cfg = fw.config.get('monitor') or {}
    if cfg.get('enabled', True) is False:
        ctx.log("系统监控已禁用 (monitor.enabled: false)")
        return

    _service = MonitorService(ctx.zkg_tool('sysres'), log=ctx.log,
                              min_interval=float(cfg.get('min_interval') or 1.0))
    fw.monitor = _service
    fw.services.register('monitor', _service)

    # ── 历史采样（history_enabled: false 可关）────────────────────
    if cfg.get('history_enabled', True) is not False:
        _recorder = HistoryRecorder(
            fw, _service,
            interval=int(cfg.get('history_interval') or 60),
            days=int(cfg.get('history_days') or 30))
        _recorder.start()

    def _h_history(a):
        if _recorder is None:
            return {'ok': False, 'data': '历史采样未启用 (monitor.history_enabled: false)'}
        return {'ok': True, 'data': _recorder.query(float((a or {}).get('hours') or 24))}

    # ── 数据源：随心跳上报（多机时中心侧就能看到各节点负载）──
    def _provider():
        s = _service.snapshot()
        return {
            'cpu': s.get('cpu', {}).get('percent'),
            'memory': s.get('memory', {}).get('percent'),
            'uptime': s.get('uptime'),
            'backend': s.get('info', {}).get('backend'),
            'disks': [{'mountpoint': d.get('mountpoint'), 'percent': d.get('percent')}
                      for d in (s.get('disks') or [])],
        }

    fw.nodes.register_provider('sysres', _provider, desc='主机资源', level='software')

    # ── 命令：远程节点可读自己的负载 ──
    fw.nodes.register_handler(
        'sysres.snapshot',
        lambda a: {'ok': True, 'data': _service.snapshot()}, desc='主机快照', level='software')
    fw.nodes.register_handler(
        'sysres.top',
        lambda a: {'ok': True, 'data': _service.top((a or {}).get('n', 10),
                                                    (a or {}).get('sort', 'cpu'))},
        desc='进程 TOP', level='software')
    fw.nodes.register_handler(
        'monitor.history', _h_history, desc='历史趋势', level='software')

    from flask import jsonify, request as _req

    def _api_host():
        return jsonify({'ok': True, **_service.snapshot()})

    def _api_top():
        n = _req.args.get('n') or 10
        sort = _req.args.get('sort') or 'cpu'
        return jsonify({'ok': True, **_service.top(int(n), sort)})

    def _api_info():
        return jsonify({'ok': True, **_service.info()})

    ctx.register_api('/api/system/host', _api_host, methods=['GET'],
                     description='主机资源快照')
    ctx.register_api('/api/system/host/top', _api_top, methods=['GET'],
                     description='进程 TOP')
    ctx.register_api('/api/system/host/info', _api_info, methods=['GET'])

    info = _service.info()
    ctx.log(f"系统监控已就绪（后端 {info.get('backend')}，"
            f"{info.get('system')} {info.get('release')}，"
            f"CPU {info.get('cpu_count')} 核）")
