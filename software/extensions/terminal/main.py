# -*- coding: utf-8 -*-
"""
在线终端（官方扩展）

运维面板的「最后一公里」：能敲命令。设计上刻意**不做成完整 PTY**——
交互式全屏程序（vim/top）在 Web 上体验很差，且 PTY 在不同平台的实现差异巨大。
这里做的是**命令模式**：

- 每条命令独立执行，带上限时与输出截断；
- `cd` 由面板托管：不真正执行，只把新工作目录回给前端，于是「目录感」是连续的；
- 执行历史留在内存（最近 N 条），可回看、可复制；
- 输出超出上限时截断并标注，避免一次 `cat` 大文件把面板打爆。

**执行本身全部交给 `procs` 机制包**：进程组标志、超时整树终止、编码自适配、
输出截断都只在那里实现一次 —— 本模块不再 import subprocess。

安全：全部接口走内核鉴权（登录 / API Key），并受 `terminal.enabled` 总开关约束。
"""
import logging
import os
import sys
import threading
import time

logger = logging.getLogger('zernus')

__plugin_meta__ = {
    "name": "在线终端",
    "version": "0.2.0",
    "author": "ZPanel",
    "desc": "命令模式终端：限时执行、整树终止、输出截断、cd 托管、历史可回看",
    "priority": 45,
    "official": True,
}

MAX_OUTPUT = 200 * 1024             # 单次输出上限（字节）
MAX_HISTORY = 200                   # 保留的历史条数


class TerminalService:
    """命令执行与历史。"""

    def __init__(self, default_cwd: str = '', default_timeout: int = 60, log=None,
                 procs=None):
        self._log = log or (lambda m: logger.info(m))
        if procs is None:
            raise RuntimeError(
                "在线终端依赖 procs 机制包，但未被加载 —— "
                "检查 software/extensions/terminal/manifest.toml 的 dependencies")
        self.procs = procs
        self.default_cwd = default_cwd or os.path.expanduser('~')
        self.default_timeout = max(1, int(default_timeout))
        self._history = []
        self._lock = threading.Lock()

    # ── 执行 ──────────────────────────────────────────────
    def exec(self, cmd: str, cwd: str = '', timeout: int = 0) -> dict:
        cmd = str(cmd or '').strip()
        if not cmd:
            return {'ok': False, 'error': '命令为空'}
        cwd = cwd or self.default_cwd
        if not os.path.isdir(cwd):
            cwd = os.path.expanduser('~')

        # cd 托管：只改「当前目录」，不启动进程
        cd = self._handle_cd(cmd, cwd)
        if cd is not None:
            self._record(cmd, cwd, cd, 0)
            return cd

        timeout = int(timeout or 0) or self.default_timeout
        r = self.procs.run(cmd, cwd=cwd, timeout=timeout, shell=True,
                           max_output=MAX_OUTPUT)

        err = r.stderr or ''
        if r.timed_out:
            err += f'\n[zpanel] 执行超时（{timeout}s），已整树终止\n'
        if r.truncated:
            err += f'\n…[zpanel] 输出超过 {MAX_OUTPUT // 1024}KB，仅保留末尾\n'

        res = {
            'ok': True,
            'stdout': r.stdout or '',
            'stderr': err,
            'code': -9 if r.timed_out else r.returncode,
            'cwd': cwd,
            'duration_ms': int(r.duration * 1000),
            'truncated': bool(r.truncated),
            # 注意：这里不能叫 `timeout`（前端当布尔判断"是否超时"会永远为真）；
            # 是否超时看 timed_out，超时提示已经在 stderr 里。
            'timed_out': bool(r.timed_out),
            'timeout_limit': timeout,
        }
        self._record(cmd, cwd, res, res['duration_ms'])
        return res

    @staticmethod
    def _handle_cd(cmd: str, cwd: str):
        """cd 交给面板处理：返回新的 cwd，而不是启动一个必然退出的 shell。"""
        s = cmd.strip().rstrip(';').strip()
        if s != 'cd' and not s.startswith('cd '):
            return None
        target = s[2:].strip().strip('"\'') if len(s) > 2 else ''
        if not target or target == '~':
            target = os.path.expanduser('~')
        new = target if os.path.isabs(target) else os.path.join(cwd, target)
        new = os.path.realpath(new)
        if os.path.isdir(new):
            return {'ok': True, 'stdout': '', 'stderr': '', 'code': 0,
                    'cwd': new, 'duration_ms': 0, 'cd': True}
        return {'ok': False, 'stdout': '', 'stderr': f'cd: 目录不存在: {target}\n',
                'code': 1, 'cwd': cwd, 'duration_ms': 0}

    # ── 历史 ──────────────────────────────────────────────
    def _record(self, cmd: str, cwd: str, res: dict, duration_ms: int):
        with self._lock:
            self._history.append({
                'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
                'cmd': cmd, 'cwd': cwd,
                'code': res.get('code'),
                'duration_ms': duration_ms,
                'failed': bool(res.get('error')) or (res.get('code') not in (0, None)),
            })
            if len(self._history) > MAX_HISTORY:
                self._history = self._history[-MAX_HISTORY:]

    def history(self, limit: int = 50) -> list:
        with self._lock:
            return list(self._history)[-int(limit):]

    def clear_history(self) -> dict:
        with self._lock:
            self._history.clear()
        return {'ok': True}

    def info(self) -> dict:
        return {
            'platform': sys.platform,
            'shell': 'cmd.exe' if os.name == 'nt' else '/bin/sh',
            'default_cwd': self.default_cwd,
            'timeout': self.default_timeout,
            'history': len(self._history),
            'max_output_kb': MAX_OUTPUT // 1024,
        }


_service = None


def register(ctx):
    global _service
    fw = ctx._framework
    cfg = fw.config.get('terminal') or {}
    if cfg.get('enabled', True) is False:
        ctx.log("在线终端已禁用 (terminal.enabled: false)")
        return

    _service = TerminalService(
        default_cwd=str(cfg.get('default_cwd') or os.path.expanduser('~')),
        default_timeout=int(cfg.get('timeout') or 60),
        log=ctx.log,
        procs=ctx.zkg_tool('procs'),
    )
    fw.terminal_svc = _service
    fw.services.register('terminal_svc', _service)

    # 内核命令：远程节点可下发命令执行
    fw.nodes.register_handler(
        'terminal.exec',
        lambda a: (lambda r: {'ok': r.get('ok', False), 'data': r})(
            _service.exec((a or {}).get('cmd', ''),
                          (a or {}).get('cwd', ''),
                          (a or {}).get('timeout', 0))),
        desc='执行命令', level='software')

    from flask import jsonify, request as _req

    def _api_exec():
        b = _req.get_json(silent=True) or {}
        res = _service.exec(b.get('cmd', ''), b.get('cwd', ''), b.get('timeout', 0))
        status = 200 if res.get('ok') else 400
        return jsonify({'ok': bool(res.get('ok')), **res}), status

    def _api_history():
        limit = int(_req.args.get('limit') or 50)
        return jsonify({'ok': True, 'history': _service.history(limit)})

    def _api_clear():
        return jsonify({'ok': True, **_service.clear_history()})

    def _api_info():
        return jsonify({'ok': True, **_service.info()})

    ctx.register_api('/api/terminal/exec', _api_exec, methods=['POST'],
                     description='执行命令')
    ctx.register_api('/api/terminal/history', _api_history, methods=['GET'])
    ctx.register_api('/api/terminal/history', _api_clear, methods=['DELETE'])
    ctx.register_api('/api/terminal/info', _api_info, methods=['GET'])

    ctx.log(f"在线终端已就绪（shell={'cmd.exe' if os.name == 'nt' else '/bin/sh'}，"
            f"默认超时 {_service.default_timeout}s，执行器 procs 机制包）")
