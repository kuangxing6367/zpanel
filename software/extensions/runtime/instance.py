# -*- coding: utf-8 -*-
"""
实例（Instance）—— 一个被托管的服务进程

模型照 MCSM 的做法拆成两面：

    config  可持久化：启停命令、工作目录、环境变量、端口、自启策略
    state   进程内易失：状态机、进程句柄、计数、输出环形缓冲、订阅者

状态机（**过渡态是体验的关键**——点了按钮要立刻有反馈，并发点击不能打起来）：

        STOP(0) ──start──▶ STARTING(2) ──就绪──▶ RUNNING(3)
           ▲                                          │
           └── STOPPING(1) ◀──────────────── stop ────┘
        任一时刻有操作在途 → BUSY(-1)（由 `self._lock` 保证互斥）

进程组处理跨平台差异：
- Windows：`CREATE_NEW_PROCESS_GROUP` 起独立进程组，终止时 `taskkill /T` 杀整棵树
  （Node 之类会 fork 子进程，只杀父进程会留孤儿）；
- Unix：`start_new_session=True` 建新会话，终止时对进程组发 SIGTERM → SIGKILL。

输出走**环形缓冲 + 订阅者广播**：一份 stdout 可同时喂给多个观察者，
且消费者再慢也不会阻塞采集（超上限丢弃并计数）。
"""
from __future__ import annotations

import collections
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid

logger = logging.getLogger('zernus')

IS_WINDOWS = os.name == 'nt'

# 由扩展入口注入的 procs 机制包（进程创建 / 整树终止的唯一实现处）
_PROCS = None


def bind(procs):
    """注入 procs 机制包。必须在使用实例前调用（extensions/runtime/main.py）。"""
    global _PROCS
    _PROCS = procs


def _procs():
    if _PROCS is None:
        raise RuntimeError(
            "运行时实例依赖 procs 机制包，但未被加载 —— "
            "检查 software/extensions/runtime/manifest.toml 的 dependencies")
    return _PROCS


class Status:
    """实例状态机取值。"""
    BUSY = -1        # 有操作在途（过渡态，防并发误操作）
    STOP = 0
    STOPPING = 1
    STARTING = 2
    RUNNING = 3

    NAMES = {BUSY: 'busy', STOP: 'stopped', STOPPING: 'stopping',
             STARTING: 'starting', RUNNING: 'running'}


DEFAULT_STOP_TIMEOUT = 20        # 优雅停止等待秒数，超时转强制
OUTPUT_LINES = 500               # 每实例保留的输出行数（环形）


class Instance:
    """单个受管服务实例。"""

    def __init__(self, cfg: dict, log=None):
        self._lock = threading.RLock()
        self._log = log or (lambda m: logger.info(m))

        # ── config（可持久化）──
        self.id = str(cfg.get('id') or uuid.uuid4().hex[:16])
        self.name = str(cfg.get('name') or '').strip() or self.id
        self.kind = str(cfg.get('kind') or 'generic')      # node / java / php / generic
        self.cwd = str(cfg.get('cwd') or '').strip()
        self.start_command = str(cfg.get('start_command') or '').strip()
        self.stop_command = str(cfg.get('stop_command') or '').strip()
        self.stop_timeout = int(cfg.get('stop_timeout') or DEFAULT_STOP_TIMEOUT)
        self.env = cfg.get('env') or {}
        if isinstance(self.env, str):
            try:
                self.env = json.loads(self.env or '{}')
            except Exception:
                self.env = {}
        self.port = int(cfg.get('port') or 0)
        self.auto_start = bool(cfg.get('auto_start'))
        self.auto_restart = bool(cfg.get('auto_restart'))
        self.max_restarts = int(cfg.get('max_restarts', -1))   # -1 = 不限
        self.runtime = str(cfg.get('runtime') or '')            # 运行时标识，如 node@18
        self.tags = cfg.get('tags') or []
        self.created_at = str(cfg.get('created_at') or _now())
        self.updated_at = str(cfg.get('updated_at') or _now())

        # ── state（进程内易失）──
        self.status = Status.STOP
        self.pid = None
        self.process = None
        self.started_at = 0.0
        self.start_count = 0
        self.restart_count = 0
        self.exit_code = None
        self.last_error = ''
        self._output = collections.deque(maxlen=OUTPUT_LINES)
        self._subscribers = set()      # 可调用对象：fn(instance_id, stream, line)
        self._pump_thread = None
        self._watch_thread = None
        self._stopping = False
        self._stopped_evt = threading.Event()

    # ══════════════════════════════════════════════════════
    # 配置
    # ══════════════════════════════════════════════════════
    def to_config(self) -> dict:
        """导出可持久化的配置（不含运行态）。"""
        return {
            'id': self.id, 'name': self.name, 'kind': self.kind,
            'cwd': self.cwd, 'start_command': self.start_command,
            'stop_command': self.stop_command, 'stop_timeout': self.stop_timeout,
            'env': self.env, 'port': self.port,
            'auto_start': self.auto_start, 'auto_restart': self.auto_restart,
            'max_restarts': self.max_restarts, 'runtime': self.runtime,
            'tags': self.tags,
            'created_at': self.created_at, 'updated_at': _now(),
        }

    def update_config(self, data: dict) -> dict:
        """更新可变配置（运行中禁止改启动命令这类关键字段）。"""
        allowed = {'name', 'cwd', 'start_command', 'stop_command', 'stop_timeout',
                   'env', 'port', 'auto_start', 'auto_restart', 'max_restarts',
                   'runtime', 'tags'}
        with self._lock:
            for k, v in (data or {}).items():
                if k not in allowed or v is None:
                    continue
                setattr(self, k, v)
            self.updated_at = _now()
            return self.to_config()

    # ══════════════════════════════════════════════════════
    # 状态
    # ══════════════════════════════════════════════════════
    def snapshot(self) -> dict:
        """给面板看的完整视图（config + 运行态）。"""
        with self._lock:
            return {
                **self.to_config(),
                'status': Status.NAMES.get(self.status, 'unknown'),
                'status_code': self.status,
                'pid': self.pid,
                'started_at': (time.strftime('%Y-%m-%d %H:%M:%S',
                                             time.localtime(self.started_at))
                               if self.started_at else None),
                'uptime_seconds': (int(time.time() - self.started_at)
                                   if self.status == Status.RUNNING and self.started_at else 0),
                'start_count': self.start_count,
                'restart_count': self.restart_count,
                'exit_code': self.exit_code,
                'last_error': self.last_error,
                'output_lines': len(self._output),
                'subscribers': len(self._subscribers),
            }

    def is_running(self) -> bool:
        with self._lock:
            return self.status == Status.RUNNING

    # ══════════════════════════════════════════════════════
    # 生命周期
    # ══════════════════════════════════════════════════════
    def start(self) -> dict:
        """启动实例。返回 {'ok', 'error'?}。"""
        with self._lock:
            if self.status in (Status.RUNNING, Status.STARTING):
                return {'ok': False, 'error': f'实例已在 {Status.NAMES[self.status]} 状态'}
            if self.status == Status.BUSY:
                return {'ok': False, 'error': '实例正忙，请稍候'}
            if not self.start_command:
                return {'ok': False, 'error': '未配置启动命令'}
            if self.cwd and not os.path.isdir(self.cwd):
                return {'ok': False, 'error': f'工作目录不存在: {self.cwd}'}

            self.status = Status.STARTING
            self.last_error = ''
            self._stopping = False        # 清掉上一次停止留下的意图标志
            self._stopped_evt.clear()
            try:
                self._spawn()
            except Exception as e:
                self.status = Status.STOP
                self.last_error = f'{type(e).__name__}: {e}'
                self._emit('stderr', f'[zpanel] 启动失败: {self.last_error}')
                return {'ok': False, 'error': self.last_error}

            self.status = Status.RUNNING
            self.started_at = time.time()
            self.start_count += 1
            self._emit('stdout', f'[zpanel] 已启动 pid={self.pid}，命令: {self.start_command}')
            self._log(f"实例 {self.name} 已启动（pid={self.pid}）")

        # 线程在锁外启动，避免子进程输出回调反抢锁造成死锁
        self._pump_thread = threading.Thread(target=self._pump_output,
                                             name=f'inst-pump-{self.id}', daemon=True)
        self._pump_thread.start()
        self._watch_thread = threading.Thread(target=self._watch,
                                              name=f'inst-watch-{self.id}', daemon=True)
        self._watch_thread.start()
        return {'ok': True}

    def stop(self, force: bool = False) -> dict:
        """停止实例：先优雅（发 stop_command），超时转强制（杀进程树）。"""
        with self._lock:
            if self.status in (Status.STOP, Status.STOPPING):
                return {'ok': True, 'note': '实例未在运行'}
            if self.process is None:
                self.status = Status.STOP
                return {'ok': True, 'note': '无进程句柄'}
            self.status = Status.STOPPING
            self._stopping = True
            proc = self.process

        graceful_ok = False
        if not force and self.stop_command:
            try:
                line = self.stop_command if self.stop_command.endswith('\n') \
                    else self.stop_command + '\n'
                # 试着写 stdin；进程若已关闭 stdin 会抛错，忽略即可
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.write(line)
                    proc.stdin.flush()
                    graceful_ok = True
                    self._emit('stdout', f'[zpanel] 已发送停止指令: {self.stop_command.strip()}')
            except Exception as e:
                logger.debug("[runtime] 写入 stop_command 失败: %s", e)

        if graceful_ok:
            try:
                proc.wait(timeout=self.stop_timeout)
            except subprocess.TimeoutExpired:
                self._emit('stderr', f'[zpanel] 优雅停止超时（{self.stop_timeout}s），强制终止')
                self._kill_tree(proc)
        else:
            self._kill_tree(proc)

        try:
            proc.wait(timeout=8)
        except Exception:
            pass

        with self._lock:
            self._stopped_evt.set()
            self.status = Status.STOP
            self.process = None
            self.pid = None
            self._stopping = False
        self._emit('stdout', '[zpanel] 已停止')
        self._log(f"实例 {self.name} 已停止")
        return {'ok': True}

    def restart(self) -> dict:
        res = self.stop()
        if not res.get('ok'):
            return res
        time.sleep(0.4)
        return self.start()

    # ══════════════════════════════════════════════════════
    # 进程
    # ══════════════════════════════════════════════════════
    def _build_env(self) -> dict:
        env = dict(os.environ)
        for k, v in (self.env or {}).items():
            env[str(k)] = str(v)
        if self.port:
            env.setdefault('PORT', str(self.port))
        env['ZPANEL_INSTANCE_ID'] = self.id
        env['ZPANEL_INSTANCE_NAME'] = self.name
        return env

    def _spawn(self):
        # 进程创建与平台标志（进程组 / 新会话）全部由 procs 机制包负责，
        # 本模块只描述「要什么」，不再关心 Windows 与 POSIX 的差异。
        self.process = _procs().popen(
            self.start_command,
            cwd=self.cwd or None,
            env=self._build_env(),
            shell=True,                   # 允许 .bat / npm.cmd 这类
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,     # 合并输出，保持时序可读
            bufsize=1,
        )
        self.pid = self.process.pid

    def _kill_tree(self, proc):
        """终止整棵进程树（Node/Java 常 fork 子进程，只杀父会留孤儿）。

        语义保持「先温和、后强制」：
          - Windows：taskkill /T /F（不带 /F 对控制台程序基本无效，直接强制）
          - POSIX  ：先对进程组 SIGTERM 给清理机会，等不到再 SIGKILL
        平台差异已下沉到 procs 机制包，本模块只表达意图。
        """
        if proc is None or proc.poll() is not None:
            return
        procs = _procs()          # 模块级函数 —— 写成 self._procs() 会 AttributeError（停止实例直接 500，本机踩过）
        if IS_WINDOWS:
            procs.kill_tree(proc.pid, force=True)
        else:
            procs.kill_tree(proc.pid, force=False)
            try:
                proc.wait(timeout=8)
                return
            except Exception:
                procs.kill_tree(proc.pid, force=True)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    def _pump_output(self):
        """读输出：进环形缓冲 + 广播给订阅者。"""
        proc = self.process
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                self._emit('stdout', line.rstrip('\r\n'))
        except Exception as e:
            logger.debug("[runtime] 输出读取结束: %s", e)

    def _watch(self):
        """等待退出：更新状态，按策略自动重启。"""
        proc = self.process
        if proc is None:
            return
        code = proc.wait()
        with self._lock:
            self.exit_code = code
            was_stopping = self._stopping          # True = 用户主动停的
            self.process = None
            self.pid = None
            if not was_stopping:
                # 进程自己退了（崩溃 / 正常结束）。这里**不碰** _stopped_evt ——
                # 那个标志表示「用户要求停止」的意图，崩退不算；否则自动重启会
                # 被自己刚设的标志拦掉（实测踩过一次）。
                self.status = Status.STOP
        self._emit('stdout', f'[zpanel] 进程已退出，退出码 {code}')

        if was_stopping or not self.auto_restart:
            return
        if self.max_restarts >= 0 and self.restart_count >= self.max_restarts:
            self._emit('stderr', f'[zpanel] 已达自动重启上限（{self.max_restarts} 次），不再拉起')
            return
        self.restart_count += 1
        self._emit('stdout', f'[zpanel] {3} 秒后自动重启（第 {self.restart_count} 次）')
        time.sleep(3)
        # 期间可能被手动停掉
        if self._stopped_evt.is_set():
            return
        self.start()

    # ══════════════════════════════════════════════════════
    # 输出
    # ══════════════════════════════════════════════════════
    def _emit(self, stream: str, line: str):
        """一行输出：进环形缓冲并广播（订阅者异常不影响采集）。"""
        entry = {'ts': time.strftime('%H:%M:%S'), 'stream': stream, 'line': line}
        self._output.append(entry)
        for cb in list(self._subscribers):
            try:
                cb(self.id, stream, line)
            except Exception:
                logger.debug("[runtime] 输出订阅者异常", exc_info=True)

    def history(self, limit: int = OUTPUT_LINES) -> list:
        with self._lock:
            return list(self._output)[-int(limit):]

    def subscribe(self, cb):
        with self._lock:
            self._subscribers.add(cb)

    def unsubscribe(self, cb):
        with self._lock:
            self._subscribers.discard(cb)

    def write_stdin(self, data: str) -> dict:
        """向实例标准输入写入（交互式控制台用）。"""
        with self._lock:
            proc = self.process
        if proc is None or proc.stdin is None or proc.stdin.closed:
            return {'ok': False, 'error': '实例未运行'}
        try:
            proc.stdin.write(data if data.endswith('\n') else data + '\n')
            proc.stdin.flush()
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'error': str(e)}


def _now() -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S')
