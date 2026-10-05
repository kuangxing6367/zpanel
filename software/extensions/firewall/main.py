# -*- coding: utf-8 -*-
"""
防火墙管理（官方扩展）

**管系统自己的防火墙，不自己实现包过滤**。按平台探测可用的管理器：

| 平台 | 管理器 |
| --- | --- |
| Windows | `netsh advfirewall` |
| Linux | `ufw` → `firewall-cmd`（firewalld）→ `iptables`，按序探测 |

只做四件事：查状态、列规则、总开关、按端口增删放行规则。
**权限不足是被预期的结果**（firewall-cmd/iptables 都要 root），统一优雅降级。
执行全部走 `procs` 机制包（超时整树终止、编码判别）。
"""
import logging
import os

logger = logging.getLogger('zernus')

__plugin_meta__ = {
    "name": "防火墙管理",
    "version": "0.1.0",
    "author": "ZPanel",
    "desc": "管系统防火墙：状态/开关/规则列表/按端口放行（netsh · ufw · firewalld · iptables）",
    "priority": 50,
    "official": True,
}

_svc = None


class FirewallService:
    """跨平台防火墙外壳。平台分支收敛在 backend() 一处。"""

    def __init__(self, log=None, procs=None, probe=None):
        if procs is None:
            raise RuntimeError("防火墙依赖 procs 机制包（检查 manifest.toml 的 dependencies）")
        if probe is None:
            raise RuntimeError("防火墙依赖 probe 机制包（检查 manifest.toml 的 dependencies）")
        self.procs = procs
        self.probe = probe          # 找可执行文件统一走 probe.which（本扩展不自带）
        self._log = log or (lambda m: logger.info(m))
        self._backend = None

    # ── 后端探测 ────────────────────────────────────────────
    def backend(self) -> dict:
        """探测本机可用的防火墙后端（找可执行文件走 probe 机制包）。"""
        if self._backend:
            return self._backend
        if os.name == 'nt':
            exe = self.probe.which('netsh') or ''
            b = {'id': 'netsh', 'exe': exe, 'available': bool(exe)}
        else:
            b = {'id': 'none', 'exe': '', 'available': False}
            for bid, exe in (('ufw', 'ufw'), ('firewalld', 'firewall-cmd'),
                             ('iptables', 'iptables')):
                p = self.probe.which(exe)
                if p:
                    b = {'id': bid, 'exe': p, 'available': True}
                    break
        self._backend = b
        return b

    def _run(self, argv: list, timeout: float = 15.0) -> dict:
        r = self.procs.run(argv, timeout=timeout, shell=False)
        raw = ((r.stdout or '') + (r.stderr or '')).strip()
        return {'code': r.returncode, 'raw': raw[:4000], 'timed_out': r.timed_out}

    # ── 状态与开关 ──────────────────────────────────────────
    def status(self) -> dict:
        b = self.backend()
        out = {'backend': b['id'], 'available': b['available'], 'enabled': None, 'raw': ''}
        if not b['available']:
            out['error'] = '未找到可用的防火墙管理器（netsh / ufw / firewall-cmd / iptables）'
            return out
        if b['id'] == 'netsh':
            r = self._run(['netsh', 'advfirewall', 'show', 'allprofiles', 'state'])
            out['raw'] = r['raw']
            # 输出形如「状态                         ON」/「State                       ON」
            low = r['raw'].lower()
            on_cnt = low.count(' on\n') + low.endswith(' on') + low.count('启用') 
            off_cnt = low.count(' off\n') + low.endswith(' off') + low.count('禁用')
            out['enabled'] = on_cnt >= off_cnt if (on_cnt or off_cnt) else None
        elif b['id'] == 'ufw':
            r = self._run(['ufw', 'status'])
            out['raw'] = r['raw']
            out['enabled'] = 'status: active' in r['raw'].lower()
        elif b['id'] == 'firewalld':
            r = self._run(['firewall-cmd', '--state'])
            out['raw'] = r['raw']
            out['enabled'] = 'running' in r['raw'].lower()
        else:  # iptables：只能看有没有规则，开关概念不存在
            r = self._run(['iptables', '-L', '-n'])
            out['raw'] = r['raw']
            out['enabled'] = bool(r['raw'].strip())
        return out

    def set_enabled(self, on: bool) -> dict:
        b = self.backend()
        if not b['available']:
            return {'ok': False, 'error': '未找到可用的防火墙管理器'}
        if b['id'] == 'netsh':
            argv = ['netsh', 'advfirewall', 'set', 'allprofiles', 'state',
                    'on' if on else 'off']
        elif b['id'] == 'ufw':
            argv = ['ufw', '--force', 'enable' if on else 'disable']
        elif b['id'] == 'firewalld':
            return {'ok': False, 'error': 'firewalld 的启停请走「服务管理」（svcmgr），避免双份实现'}
        else:
            return {'ok': False, 'error': 'iptables 没有「总开关」概念'}
        r = self._run(argv, timeout=25)
        ok = r['code'] == 0
        return {'ok': ok, 'raw': r['raw'][:400],
                'error': '' if ok else (r['raw'][:300] or f'退出码 {r["code"]}（权限不足？）')}

    # ── 规则 ────────────────────────────────────────────────
    def rules(self) -> dict:
        b = self.backend()
        if not b['available']:
            return {'ok': False, 'error': '未找到可用的防火墙管理器', 'rules': []}
        if b['id'] == 'netsh':
            r = self._run(['netsh', 'advfirewall', 'firewall', 'show', 'rule',
                           'name=all', 'dir=in'])
            return {'ok': r['code'] == 0, 'backend': 'netsh', 'rules': [],
                    'raw': r['raw'], 'error': '' if r['code'] == 0 else r['raw'][:200]}
        if b['id'] == 'ufw':
            r = self._run(['ufw', 'status', 'numbered'])
            rules = []
            for line in r['raw'].splitlines():
                if '] ' in line and ('ALLOW' in line or 'DENY' in line or 'REJECT' in line):
                    rules.append(line.strip())
            return {'ok': True, 'backend': 'ufw', 'rules': rules, 'raw': ''}
        if b['id'] == 'firewalld':
            r = self._run(['firewall-cmd', '--list-ports'])
            ports = [p for p in r['raw'].split() if p]
            return {'ok': True, 'backend': 'firewalld', 'rules': ports, 'raw': ''}
        r = self._run(['iptables', '-L', '-n', '--line-numbers'])
        return {'ok': r['code'] == 0, 'backend': 'iptables', 'rules': [],
                'raw': r['raw'][:4000]}

    def allow_port(self, port: int, proto: str = 'TCP', name: str = '', remove: bool = False) -> dict:
        """按端口放行/撤销（增删对称，面板上一个开关搞定）。"""
        port = int(port)
        proto = str(proto).upper()
        if proto not in ('TCP', 'UDP'):
            return {'ok': False, 'error': f'协议只支持 TCP/UDP，收到 {proto}'}
        b = self.backend()
        if not b['available']:
            return {'ok': False, 'error': '未找到可用的防火墙管理器'}
        if b['id'] == 'netsh':
            rule_name = name or f'zpanel-port-{port}-{proto}'
            base = ['netsh', 'advfirewall', 'firewall']
            if remove:
                argv = base + ['delete', 'rule', f'name={rule_name}']
            else:
                argv = base + ['add', 'rule', f'name={rule_name}', 'dir=in',
                               'action=allow', f'protocol={proto}', f'localport={port}']
        elif b['id'] == 'ufw':
            argv = ['ufw', 'delete' if remove else 'allow', f'{port}/{proto.lower()}']
        elif b['id'] == 'firewalld':
            argv = ['firewall-cmd', '--permanent',
                    '--remove-port' if remove else '--add-port', f'{port}/{proto.lower()}',
                    '--reload']
        else:
            return {'ok': False, 'error': 'iptables 的细粒度规则请直接用终端操作'}
        r = self._run(argv, timeout=25)
        ok = r['code'] == 0
        self._log(f"防火墙{'撤销' if remove else '放行'} {port}/{proto}: {'成功' if ok else r['raw'][:120]}")
        return {'ok': ok, 'port': port, 'proto': proto, 'raw': r['raw'][:400],
                'error': '' if ok else (r['raw'][:300] or f'退出码 {r["code"]}（权限不足？）')}


def register(ctx):
    global _svc
    fw = ctx._framework
    procs = ctx.zkg_tool('procs')
    probe = ctx.zkg_tool('probe')
    if procs is None or probe is None:
        ctx.log(f"防火墙：缺少机制包 {[n for n, m in (('procs', procs), ('probe', probe)) if m is None]}，已跳过")
        return
    _svc = FirewallService(log=ctx.log, procs=procs, probe=probe)
    fw.firewall_svc = _svc
    fw.services.register('firewall', _svc)

    def _ok(d):
        return {'ok': True, 'data': d}

    def _err(m):
        return {'ok': False, 'data': str(m)}

    for name, fn, desc in (
        ('firewall.status', lambda a: _ok(_svc.status()), '防火墙状态'),
        ('firewall.rules', lambda a: _ok(_svc.rules()), '规则列表'),
        ('firewall.enable', lambda a: _ok(_svc.set_enabled(bool((a or {}).get('on', True)))),
         '总开关'),
        ('firewall.allow', lambda a: _ok(_svc.allow_port(
            (a or {}).get('port', 0), (a or {}).get('proto', 'TCP'),
            (a or {}).get('name', ''), bool((a or {}).get('remove')))), '按端口放行/撤销'),
    ):
        fw.nodes.register_handler(name, fn, desc=desc, level='software')

    from flask import jsonify, request as _req

    def _body():
        return _req.get_json(silent=True) or {}

    ctx.register_api('/api/firewall/status', lambda: jsonify({'ok': True, **_svc.status()}),
                     methods=['GET'])
    ctx.register_api('/api/firewall/rules', lambda: jsonify({'ok': True, **_svc.rules()}),
                     methods=['GET'])
    ctx.register_api('/api/firewall/enable',
                     lambda: (lambda r: (jsonify({'ok': True, **r}) if r.get('ok')
                                         else (jsonify({'ok': False, **r}), 502)))(_svc.set_enabled(bool(_body().get('on', True)))),
                     methods=['POST'])
    ctx.register_api('/api/firewall/allow',
                     lambda: (lambda b, r: (jsonify({'ok': True, **r}) if r.get('ok')
                                            else (jsonify({'ok': False, **r}), 502)))(
                         _body(), _svc.allow_port(_body().get('port', 0),
                                                  _body().get('proto', 'TCP'),
                                                  _body().get('name', ''),
                                                  bool(_body().get('remove')))),
                     methods=['POST'])

    ctx.log(f"防火墙管理已就绪（后端 {_svc.backend()['id']}）")


def unregister():
    global _svc
    _svc = None
