# -*- coding: utf-8 -*-
"""
告警（官方扩展）

规则引擎：**规则 = 指标 + 比较符 + 阈值 + 状态记录 + 通知**。

- 指标源：sysres 机制包（cpu / mem / disk / load）+ ssl.check 的 days_left（证书到期）；
- 判定：cpu > 90 这类表达式收敛成 {metric, op, value}，不做 eval（安全边界）；
- 记录：每次 check 逐条落 alert_events（SQLite），连续命中才告警（防抖 flapping）；
- 通知：**通道化** —— alert_channels 存 SMTP / HTTP(Webhook) 配置，
  规则用 channels 字段挑通道（留空 = 全部启用通道，另兼容旧的 webhook 列）；
  投递结果逐条落 alert_deliveries（哪个通道成功、失败原因原文都留痕）。
- 发信实现取自 mailer 机制包（本扩展不自带 smtplib 逻辑）。
- 定时执行：交给 scheduler 扩展调 `alerts.check` 命令（不自己养定时器）。
"""
import json
import logging
import time
import urllib.error
import urllib.request

logger = logging.getLogger('zernus')

__plugin_meta__ = {
    "name": "告警",
    "version": "0.2.0",
    "author": "ZPanel",
    "desc": "阈值规则（CPU/内存/磁盘/负载/证书到期）+ 多通道通知（邮件/Webhook），防抖后落事件与投递记录",
    "priority": 62,
    "official": True,
}

_svc = None

METRICS = ('cpu', 'mem', 'disk', 'load', 'cert_days')
OPS = ('>', '<', '>=', '<=', '==', '!=')
CHANNEL_TYPES = ('smtp', 'webhook')
SECRET_KEYS = ('password', 'token', 'secret', 'bearer')
MASK = '••••••'


def _as_ids(v) -> list:
    """通道 id 归一：str（逗号分隔）或 list 都接受，去重保序。"""
    if not v:
        return []
    raw = str(v).replace('，', ',').split(',') if isinstance(v, str) else list(v)
    out = []
    for x in raw:
        s = str(x).strip()
        if s and s not in out:
            out.append(s)
    return out


class AlertService:
    def __init__(self, fw, log=None, sysres=None, mailer=None):
        self.fw = fw
        self._log = log or (lambda m: logger.info(m))
        self.sysres = sysres
        self.mailer = mailer
        self.ensure_tables()

    def ensure_tables(self):
        self.fw.db.execute("""
            CREATE TABLE IF NOT EXISTS alert_rules (
                id        TEXT PRIMARY KEY,
                name      TEXT NOT NULL,
                metric    TEXT NOT NULL,      -- cpu/mem/disk/load/cert_days
                target    TEXT DEFAULT '',    -- disk 盘符 / 证书巡检 host
                op        TEXT NOT NULL,      -- > < >= <= == !=
                value     REAL NOT NULL,      -- 阈值（cert_days 单位=天）
                hits      INTEGER DEFAULT 1,  -- 连续命中 N 次才告警
                webhook   TEXT DEFAULT '',    -- 兼容旧字段（等价一条匿名 http 通道）
                enabled   INTEGER DEFAULT 1,
                streak    INTEGER DEFAULT 0,  -- 当前连续命中数（防抖状态）
                firing    INTEGER DEFAULT 0,
                last_value REAL,
                last_fire_at REAL DEFAULT 0,
                created_at REAL
            )
        """)
        self.fw.db.execute("""
            CREATE TABLE IF NOT EXISTS alert_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id TEXT, rule_name TEXT,
                metric TEXT, value REAL, threshold REAL,
                level TEXT DEFAULT 'warn',
                message TEXT, created_at REAL
            )
        """)
        self.fw.db.execute("""
            CREATE TABLE IF NOT EXISTS alert_channels (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                type       TEXT NOT NULL,     -- smtp / webhook
                config     TEXT DEFAULT '{}', -- JSON 配置
                enabled    INTEGER DEFAULT 1,
                last_test_at REAL DEFAULT 0,
                last_test_ok INTEGER DEFAULT 0,
                last_error TEXT DEFAULT '',
                created_at REAL
            )
        """)
        self.fw.db.execute("""
            CREATE TABLE IF NOT EXISTS alert_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id TEXT, rule_name TEXT, level TEXT,
                channel_id TEXT, channel_name TEXT, channel_type TEXT,
                ok INTEGER, error TEXT, created_at REAL
            )
        """)
        # 老库补列（规则绑通道）
        cols = {r['name'] for r in (self.fw.db.query("PRAGMA table_info(alert_rules)") or [])}
        if 'channels' not in cols:
            self.fw.db.execute("ALTER TABLE alert_rules ADD COLUMN channels TEXT DEFAULT ''")

    # ── 规则 ────────────────────────────────────────────────
    def _row(self, r: dict) -> dict:
        return {'id': r['id'], 'name': r['name'], 'metric': r['metric'],
                'target': r['target'] or '', 'op': r['op'], 'value': r['value'],
                'hits': r['hits'] or 1, 'webhook': r['webhook'] or '',
                'channels': _as_ids((r['channels'] if 'channels' in r.keys() else '') or ''),
                'enabled': bool(r['enabled']), 'firing': bool(r['firing']),
                'streak': r['streak'] or 0, 'last_value': r['last_value'],
                'last_fire_at': r['last_fire_at'] or 0, 'created_at': r['created_at'] or 0}

    def list(self) -> dict:
        rows = self.fw.db.query("SELECT * FROM alert_rules ORDER BY created_at") or []
        return {'count': len(rows), 'rules': [self._row(r) for r in rows]}

    def get(self, rid: str):
        rows = self.fw.db.query("SELECT * FROM alert_rules WHERE id=?", (rid,)) or []
        return self._row(rows[0]) if rows else None

    def create(self, data: dict) -> dict:
        import uuid
        name = str(data.get('name') or '').strip()
        metric = str(data.get('metric') or '').strip().lower()
        op = str(data.get('op') or '').strip()
        if not name:
            return {'ok': False, 'error': '规则名不能为空'}
        if metric not in METRICS:
            return {'ok': False, 'error': f'metric 只支持 {METRICS}'}
        if op not in OPS:
            return {'ok': False, 'error': f'op 只支持 {OPS}'}
        try:
            value = float(data.get('value'))
        except (TypeError, ValueError):
            return {'ok': False, 'error': 'value 必须是数字阈值'}
        rid = uuid.uuid4().hex[:16]
        self.fw.db.execute(
            "INSERT INTO alert_rules (id, name, metric, target, op, value, hits, webhook,"
            " channels, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rid, name, metric, str(data.get('target') or ''), op, value,
             max(1, int(data.get('hits') or 1)), str(data.get('webhook') or ''),
             ','.join(_as_ids(data.get('channels'))), time.time()))
        return {'ok': True, 'rule': self.get(rid)}

    def update(self, rid: str, data: dict) -> dict:
        if not self.get(rid):
            return {'ok': False, 'error': '规则不存在'}
        sets, vals = [], []
        m = {'name': 'name', 'target': 'target', 'op': 'op', 'webhook': 'webhook'}
        for k, col in m.items():
            if data.get(k) is not None:
                sets.append(f'{col}=?'); vals.append(str(data[k]))
        if data.get('channels') is not None:
            sets.append('channels=?'); vals.append(','.join(_as_ids(data['channels'])))
        if data.get('value') is not None:
            sets.append('value=?'); vals.append(float(data['value']))
        if data.get('hits') is not None:
            sets.append('hits=?'); vals.append(max(1, int(data['hits'])))
        if data.get('enabled') is not None:
            sets.append('enabled=?'); vals.append(1 if data['enabled'] else 0)
        if sets:
            vals.append(rid)
            self.fw.db.execute(f"UPDATE alert_rules SET {','.join(sets)} WHERE id=?", tuple(vals))
        return {'ok': True, 'rule': self.get(rid)}

    def remove(self, rid: str) -> dict:
        if not self.get(rid):
            return {'ok': False, 'error': '规则不存在'}
        self.fw.db.execute("DELETE FROM alert_rules WHERE id=?", (rid,))
        return {'ok': True}

    # ── 通道 ────────────────────────────────────────────────
    def _chan(self, r: dict) -> dict:
        try:
            cfg = json.loads(r['config'] or '{}')
        except (ValueError, TypeError):
            cfg = {}
        pub = dict(cfg)          # 密钥类字段一律不回显（只告诉前端"已配置"）
        for k in SECRET_KEYS:
            if pub.get(k):
                pub[k] = MASK
        return {'id': r['id'], 'name': r['name'], 'type': r['type'],
                'config': pub,
                'has_secret': any(cfg.get(k) for k in SECRET_KEYS),
                'enabled': bool(r['enabled']),
                'last_test_at': r['last_test_at'] or 0,
                'last_test_ok': bool(r['last_test_ok']),
                'last_error': r['last_error'] or '',
                'created_at': r['created_at'] or 0}

    def _chan_raw(self, cid: str):
        rows = self.fw.db.query("SELECT * FROM alert_channels WHERE id=?", (cid,)) or []
        return rows[0] if rows else None

    def channels(self) -> dict:
        rows = self.fw.db.query("SELECT * FROM alert_channels ORDER BY created_at") or []
        return {'count': len(rows), 'channels': [self._chan(r) for r in rows]}

    def channel_save(self, data: dict) -> dict:
        """新建或更新通道。config 里传掩码串表示保持原密钥不变。"""
        import uuid
        cid = str(data.get('id') or '').strip()
        name = str(data.get('name') or '').strip()
        ctype = str(data.get('type') or '').strip().lower()
        if ctype not in CHANNEL_TYPES:
            return {'ok': False, 'error': f'type 只支持 {CHANNEL_TYPES}'}
        if not name:
            return {'ok': False, 'error': '通道名不能为空'}
        cfg = data.get('config') or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except ValueError:
                return {'ok': False, 'error': 'config 不是合法 JSON'}
        if not isinstance(cfg, dict):
            return {'ok': False, 'error': 'config 必须是对象'}

        if cid:
            old = self._chan_raw(cid)
            if not old:
                return {'ok': False, 'error': '通道不存在'}
            try:
                merged = json.loads(old['config'] or '{}')
            except (ValueError, TypeError):
                merged = {}
            for k, v in cfg.items():
                if v == MASK:        # 占位符 = 不改动该密钥
                    continue
                merged[k] = v
            err = self._validate(ctype, merged)
            if err:
                return {'ok': False, 'error': err}
            self.fw.db.execute(
                "UPDATE alert_channels SET name=?, type=?, config=?, enabled=?, last_error=''"
                " WHERE id=?",
                (name, ctype, json.dumps(merged, ensure_ascii=False),
                 1 if data.get('enabled', True) else 0, cid))
        else:
            err = self._validate(ctype, cfg)
            if err:
                return {'ok': False, 'error': err}
            cid = uuid.uuid4().hex[:16]
            self.fw.db.execute(
                "INSERT INTO alert_channels (id, name, type, config, enabled, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (cid, name, ctype, json.dumps(cfg, ensure_ascii=False),
                 1 if data.get('enabled', True) else 0, time.time()))
        return {'ok': True, 'channel': next(
            (c for c in self.channels()['channels'] if c['id'] == cid), None)}

    @staticmethod
    def _validate(ctype: str, cfg: dict) -> str:
        if ctype == 'smtp':
            if not str(cfg.get('host') or '').strip():
                return 'SMTP 缺少 host'
            if not str(cfg.get('to') or '').strip():
                return 'SMTP 缺少默认收件人 to'
            if not str(cfg.get('sender') or cfg.get('user') or '').strip():
                return 'SMTP 缺少 sender（或 user）'
            return ''
        url = str(cfg.get('url') or '').strip()
        if not url:
            return 'Webhook 缺少 url'
        if not url.startswith(('http://', 'https://')):
            return 'url 必须以 http:// 或 https:// 开头'
        return ''

    def channel_remove(self, cid: str) -> dict:
        if not self._chan_raw(cid):
            return {'ok': False, 'error': '通道不存在'}
        self.fw.db.execute("DELETE FROM alert_channels WHERE id=?", (cid,))
        # 规则上的绑定一并摘掉（不留空引用）
        for r in (self.fw.db.query("SELECT id, channels FROM alert_rules") or []):
            ids = _as_ids(r['channels'])
            if cid in ids:
                ids = [x for x in ids if x != cid]
                self.fw.db.execute("UPDATE alert_rules SET channels=? WHERE id=?",
                                   (','.join(ids), r['id']))
        return {'ok': True}

    def _enabled_channels(self) -> list:
        return [self._chan(r) for r in
                (self.fw.db.query("SELECT * FROM alert_channels WHERE enabled=1") or [])]

    # ── 通道投递 ────────────────────────────────────────────
    def _deliver_one(self, ch: dict, rule: dict, level: str, subject: str,
                     body: str, raw_cfg: dict) -> dict:
        """往一个通道投递。**不抛异常**：失败转成 {ok:False, error}，由上层留痕。"""
        try:
            if ch['type'] == 'smtp':
                if self.mailer is None:
                    return {'ok': False, 'error': '缺少 mailer 机制包'}
                cfg = dict(raw_cfg)
                to = cfg.pop('to', '')
                return self.mailer.send(cfg, subject, body, to=to)
            # webhook / HTTP
            method = str(raw_cfg.get('method') or 'POST').upper()
            payload = json.dumps({
                'rule': rule['name'], 'metric': rule['metric'], 'target': rule['target'],
                'op': rule['op'], 'threshold': rule['value'], 'level': level,
                'value': rule.get('last_value'), 'message': body, 'subject': subject,
                'time': time.strftime('%Y-%m-%d %H:%M:%S'),
            }, ensure_ascii=False).encode()
            headers = {'Content-Type': 'application/json'}
            for k, v in (raw_cfg.get('headers') or {}).items():
                headers[str(k)] = str(v)
            token = str(raw_cfg.get('bearer') or raw_cfg.get('token') or '').strip()
            if token:
                headers['Authorization'] = f'Bearer {token}'
            req = urllib.request.Request(str(raw_cfg['url']), data=payload,
                                         headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=float(raw_cfg.get('timeout') or 8)) as x:
                code = int(getattr(x, 'status', 200))
                x.read(2048)
            return {'ok': 200 <= code < 300, 'status': code}
        except urllib.error.HTTPError as e:
            detail = ''
            try:
                detail = e.read(300).decode('utf-8', 'replace')
            except Exception:
                pass
            return {'ok': False, 'error': f'HTTP {e.code} {detail[:160]}'}
        except Exception as e:
            return {'ok': False, 'error': f'{type(e).__name__}: {e}'}

    def _log_delivery(self, rule, level, cid, cname, ctype, r):
        self.fw.db.execute(
            "INSERT INTO alert_deliveries (rule_id, rule_name, level, channel_id,"
            " channel_name, channel_type, ok, error, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (rule['id'], rule['name'], level, cid, cname, ctype,
             1 if r.get('ok') else 0, str(r.get('error') or ''), time.time()))

    def _notify(self, rule: dict, level: str, subject: str, body: str) -> list:
        """按规则绑定的通道投递（未绑定 = 全部启用通道）。逐条留痕。"""
        bound = rule.get('channels') or []
        chans = self._enabled_channels()
        if bound:
            chans = [c for c in chans if c['id'] in bound]
        results = []
        for ch in chans:
            raw = self._chan_raw(ch['id']) or {}
            try:
                raw_cfg = json.loads(raw.get('config') or '{}')
            except (ValueError, TypeError):
                raw_cfg = {}
            r = self._deliver_one(ch, rule, level, subject, body, raw_cfg)
            results.append({'channel': ch['name'], 'type': ch['type'],
                            'ok': bool(r.get('ok')), 'error': r.get('error', '')})
            self._log_delivery(rule, level, ch['id'], ch['name'], ch['type'], r)
            if not r.get('ok'):
                self._log(f"[告警] 通道「{ch['name']}」投递失败：{r.get('error')}")

        # 兼容旧字段：规则自带 webhook（等价一条匿名 http 通道）
        legacy = (rule.get('webhook') or '').strip()
        if legacy and not bound:
            fake = {'id': '', 'name': 'legacy-webhook', 'type': 'webhook'}
            r = self._deliver_one(fake, rule, level, subject, body, {'url': legacy})
            results.append({'channel': 'legacy-webhook', 'type': 'webhook',
                            'ok': bool(r.get('ok')), 'error': r.get('error', '')})
            self._log_delivery(rule, level, '', 'legacy-webhook', 'webhook', r)
        return results

    def channel_test(self, cid: str) -> dict:
        """真实发一条测试消息（走与告警同一段投递代码）。"""
        raw = self._chan_raw(cid)
        if not raw:
            return {'ok': False, 'error': '通道不存在'}
        ch = self._chan(raw)
        try:
            cfg = json.loads(raw['config'] or '{}')
        except (ValueError, TypeError):
            cfg = {}
        import socket as _socket
        rule = {'id': '', 'name': '通道连通性测试', 'metric': '-', 'target': '',
                'op': '-', 'value': 0, 'channels': [cid], 'last_value': None,
                'webhook': '', 'last_fire_at': 0}
        body = (f"这是一条来自 ZPanel 的通道测试消息。\n"
                f"通道：{ch['name']}（{ch['type']}）\n"
                f"主机：{_socket.gethostname()}\n"
                f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        r = self._deliver_one(ch, rule, 'test', '【ZPanel】通道测试', body, cfg)
        self.fw.db.execute(
            "UPDATE alert_channels SET last_test_at=?, last_test_ok=?, last_error=? WHERE id=?",
            (time.time(), 1 if r.get('ok') else 0, str(r.get('error') or ''), cid))
        return {'ok': bool(r.get('ok')), 'error': r.get('error', ''),
                'channel': ch['name'],
                'result': {k: v for k, v in r.items() if k != 'error'}}

    def deliveries(self, limit: int = 100) -> dict:
        rows = self.fw.db.query(
            "SELECT * FROM alert_deliveries ORDER BY id DESC LIMIT ?", (int(limit),)) or []
        return {'count': len(rows), 'deliveries': [dict(r) for r in rows]}

    # ── 指标读取 ────────────────────────────────────────────
    def _read_metric(self, rule: dict):
        metric, target = rule['metric'], rule['target']
        if metric == 'cert_days':
            if not target:
                return None, 'cert_days 规则必须填 target=巡检 host'
            from software.extensions.ssl.main import SslService  # 服务间直接复用
            r = SslService(log=self._log).check(target, 443)
            return (r.get('days_left') if r.get('ok') else None), None
        if self.sysres is None:
            return None, '缺少 sysres 机制包'
        d = self.sysres.snapshot(with_disks=True, with_net=False, top=0)
        if metric == 'cpu':
            # interval=0 依赖上次采样，冷启动首查可能得 0.0 → 恒不命中。
            # 告警判定必须拿真实值：阻塞采样 0.4s。
            return float(self.sysres.cpu_percent(0.4) or 0), None
        if metric == 'mem':
            return float((d.get('memory') or {}).get('percent') or 0), None
        if metric == 'disk':
            disks = d.get('disks') or []   # [{mountpoint, percent}, ...]
            if target:
                for it in disks:
                    if str(it.get('mountpoint')) == target:
                        return float(it['percent']), None
                return None, f'挂载点 {target} 不存在'
            return max((float(it['percent']) for it in disks), default=0.0), None
        if metric == 'load':
            load = d.get('load') or []
            return float(load[0]) if load else 0.0, None
        return None, f'未知指标 {metric}'

    @staticmethod
    def _cmp(v, op, th):
        return {'>': v > th, '<': v < th, '>=': v >= th,
                '<=': v <= th, '==': v == th, '!=': v != th}[op]

    # ── 检查 ────────────────────────────────────────────────
    def check(self, only: str = '') -> dict:
        rows = self.fw.db.query(
            "SELECT * FROM alert_rules WHERE enabled=1 ORDER BY created_at") or []
        fired, results = [], []
        for raw in rows:
            rule = self._row(raw)
            if only and rule['id'] != only and rule['name'] != only:
                continue
            value, err = self._read_metric(rule)
            if err or value is None:
                results.append({'rule': rule['name'], 'value': None,
                                'error': err or '指标不可用'})
                continue
            rule['last_value'] = value
            hit = self._cmp(value, rule['op'], rule['value'])
            streak = rule['streak'] + 1 if hit else 0
            fire = hit and streak >= rule['hits'] and not rule['firing']
            new_fire = rule['firing']
            if fire:
                new_fire = True
            elif not hit and rule['firing']:
                new_fire = False        # 恢复
                msg = f"{rule['name']} 已恢复（现值 {value:g}）"
                self.fw.db.execute(
                    "INSERT INTO alert_events (rule_id, rule_name, metric, value, threshold,"
                    " level, message, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (rule['id'], rule['name'], rule['metric'], value, rule['value'],
                     'recover', msg, time.time()))
                self._notify(rule, 'recover', f"【ZPanel】恢复：{rule['name']}", msg)
                self._log(f"告警恢复 {rule['name']}（{value:g}）")
            if fire:
                msg = (f"{rule['name']}：{rule['metric']}{rule['target'] and ':' + rule['target']}"
                       f" {rule['op']} {rule['value']:g}（现值 {value:g}，连续 {streak} 次）")
                self.fw.db.execute(
                    "INSERT INTO alert_events (rule_id, rule_name, metric, value, threshold,"
                    " level, message, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (rule['id'], rule['name'], rule['metric'], value, rule['value'],
                     'warn', msg, time.time()))
                deliv = self._notify(rule, 'warn', f"【ZPanel】告警：{rule['name']}", msg)
                self._log(f"触发告警 {msg}（通道 {len(deliv)} 条）")
                fired.append({'rule': rule['name'], 'value': value, 'message': msg,
                              'delivered': deliv})
            self.fw.db.execute(
                "UPDATE alert_rules SET streak=?, firing=?, last_value=?, last_fire_at=? WHERE id=?",
                (streak, 1 if new_fire else 0, value,
                 time.time() if fire else rule['last_fire_at'], rule['id']))
            results.append({'rule': rule['name'], 'value': value, 'hit': hit,
                            'streak': streak, 'firing': new_fire})
        return {'ok': True, 'checked': len(results), 'fired': fired, 'results': results}

    def events(self, limit: int = 100) -> dict:
        rows = self.fw.db.query(
            "SELECT * FROM alert_events ORDER BY id DESC LIMIT ?", (int(limit),)) or []
        return {'count': len(rows), 'events': [dict(r) for r in rows]}


def register(ctx):
    global _svc
    fw = ctx._framework
    sysres = ctx.zkg_tool('sysres')
    mailer = ctx.zkg_tool('mailer')
    if mailer is None:
        ctx.log("告警：缺少 mailer 机制包，邮件通道不可用")
    _svc = AlertService(fw, log=ctx.log, sysres=sysres, mailer=mailer)
    fw.alerts_svc = _svc
    fw.services.register('alerts', _svc)

    def _ok(d):
        """命令返回统一形状。

        约定（照 `core/api/routes/nodes.py` 的实际行为反推）：
        - 外层 `ok` 表示**命令执行**是否成功，为 false 时路由层会转成 HTTP 502
          并把 `data` 当错误体丢掉细节；
        - 因此「规则名重复 / url 非法」这类**业务校验失败**必须留在内层 `data.ok=False`，
          外层照样 `ok:True`；前端 `call()` 拆掉外层后自己看内层 ok。
        """
        return {'ok': True, 'data': d}

    for name, fn, desc in (
        ('alerts.list', lambda a: _ok(_svc.list()), '告警规则清单'),
        ('alerts.create', lambda a: _ok(_svc.create(a or {})), '新建规则'),
        ('alerts.update', lambda a: _ok(_svc.update((a or {}).get('id', ''), a or {})), '修改规则'),
        ('alerts.remove', lambda a: _ok(_svc.remove((a or {}).get('id', ''))), '删除规则'),
        ('alerts.check', lambda a: _ok(_svc.check(str((a or {}).get('only') or ''))),
         '执行一轮检查'),
        ('alerts.events', lambda a: _ok(_svc.events(int((a or {}).get('limit') or 100))),
         '事件历史'),
        ('alerts.channels', lambda a: _ok(_svc.channels()), '通知通道清单'),
        ('alerts.channel_save', lambda a: _ok(_svc.channel_save(a or {})), '新建/修改通道'),
        ('alerts.channel_remove', lambda a: _ok(_svc.channel_remove((a or {}).get('id', ''))),
         '删除通道'),
        ('alerts.channel_test', lambda a: _ok(_svc.channel_test((a or {}).get('id', ''))),
         '测试通道投递'),
        ('alerts.deliveries', lambda a: _ok(_svc.deliveries(int((a or {}).get('limit') or 100))),
         '投递记录'),
    ):
        fw.nodes.register_handler(name, fn, desc=desc, level='software')

    from flask import jsonify, request as _req

    def _body():
        return _req.get_json(silent=True) or {}

    def _resp(r, code=200):
        return (jsonify(r), code) if not r.get('ok') else jsonify(r)

    ctx.register_api('/api/alerts/rules', lambda: jsonify(_ok(_svc.list())), methods=['GET'])
    ctx.register_api('/api/alerts/rules', lambda: _resp(_svc.create(_body()), 400), methods=['POST'])
    ctx.register_api('/api/alerts/rules/<rid>', lambda rid: _resp(_svc.update(rid, _body()), 400),
                     methods=['PATCH'])
    ctx.register_api('/api/alerts/rules/<rid>', lambda rid: jsonify(_svc.remove(rid)),
                     methods=['DELETE'])
    ctx.register_api('/api/alerts/check', lambda: jsonify(_svc.check()), methods=['POST'])
    ctx.register_api('/api/alerts/events', lambda: jsonify(_ok(_svc.events())), methods=['GET'])
    ctx.register_api('/api/alerts/channels', lambda: jsonify(_ok(_svc.channels())), methods=['GET'])
    ctx.register_api('/api/alerts/channels', lambda: _resp(_svc.channel_save(_body()), 400),
                     methods=['POST'])
    ctx.register_api('/api/alerts/channels/<cid>',
                     lambda cid: _resp(_svc.channel_save(dict(_body(), id=cid)), 400),
                     methods=['PATCH'])
    ctx.register_api('/api/alerts/channels/<cid>', lambda cid: jsonify(_svc.channel_remove(cid)),
                     methods=['DELETE'])
    ctx.register_api('/api/alerts/channels/<cid>/test',
                     lambda cid: _resp(_svc.channel_test(cid), 400), methods=['POST'])
    ctx.register_api('/api/alerts/deliveries', lambda: jsonify(_ok(_svc.deliveries())),
                     methods=['GET'])

    ctx.log(f"告警已就绪（{_svc.list()['count']} 条规则 / "
            f"{_svc.channels()['count']} 条通道；定时执行请由 scheduler 调 alerts.check）")


def unregister():
    global _svc
    _svc = None
