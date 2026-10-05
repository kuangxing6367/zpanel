# -*- coding: utf-8 -*-
"""
多机管理统一入口（内核级）

内核在「多机」这件事上只有三个职责：

    ① 管身份   节点注册表（谁被纳管、密钥是什么、最后一次状态）
    ② 管连接   控制面（节点主动外连 hub）
    ③ 管路由   把「各层注册的数据」上报出去，把「下发的命令」交给注册者执行

**内核不管系统环境**：它不知道 Windows / Linux，不知道 PHP / Node / Java。
它只知道「服务层与软件层注册了哪些数据源和命令」。因此这一层极其薄。

注册接口（供服务层 / 软件层在启动时调用）：

    fw.nodes.register_provider('system',  fn, desc='系统指标')   # () -> dict
    fw.nodes.register_handler('runtimes.list', fn, desc='枚举运行时')  # (args) -> dict

内核自带的只有**协议级**语义（不属于任何系统环境）：
    provider : `core`     内核自身元信息（版本 / 运行时长 / 队列统计）
    handler  : `ping`     连通性探测
    handler  : `node.info` 节点元信息

装配与对外 API：

    fw.nodes.start() / stop()
    fw.nodes.list()                      # 全部纳管节点（合并在线态）
    fw.nodes.local()                     # 本机节点信息
    fw.nodes.add(name) / remove(name) / rotate_secret(name)
    fw.nodes.send_cmd(name, cmd, args)   # 下发命令（本机直接本地路由）
    fw.nodes.collect()                   # 采集本机数据（agent 上报用）
    fw.nodes.execute(cmd, args)          # 执行命令（agent 收到 CMD 时调用）

运行模式由 `config.yaml → nodes.mode` 决定：hub / agent / both / off。
"""
import logging

from .registry import NodeRegistry, gen_secret
from .hub import NodeHub
from .agent import NodeAgent

logger = logging.getLogger('zernus')


class NodeManager:
    """内核级多机管理：注册表 + 控制面 + 代理 + 数据/命令路由。"""

    def __init__(self, fw):
        self.fw = fw
        cfg = (fw.config.get('nodes') or {}) if isinstance(fw.config, dict) else {}
        self.cfg = cfg
        self.mode = str(cfg.get('mode', 'hub') or 'hub').lower()

        self.registry = NodeRegistry(fw)
        self.hub = None
        self.agent = None
        self._started = False

        # 各层注册的数据源与命令处理器（内核只持有引用，不关心实现）
        self._providers = {}    # name -> {'fn': callable, 'desc': str, 'level': str}
        self._handlers = {}     # cmd  -> {'fn': callable, 'desc': str, 'level': str}

        self._register_builtin()

    # ── 注册接口（服务层 / 软件层用）──────────────────────
    def register_provider(self, name: str, fn, desc: str = '', level: str = 'software'):
        """注册一个数据源：`fn()` 返回 dict，随心跳上报给 hub。

        :param level: 来源层级（core / service / software），仅用于展示与排障
        """
        if not callable(fn):
            raise TypeError(f'provider {name!r} 必须可调用')
        self._providers[str(name)] = {'fn': fn, 'desc': desc, 'level': level}
        logger.debug("[nodes] 数据源已注册: %s (%s)", name, level)

    def unregister_provider(self, name: str):
        self._providers.pop(str(name), None)

    def register_handler(self, cmd: str, fn, desc: str = '', level: str = 'software'):
        """注册一个命令处理器：`fn(args: dict) -> dict`，返回 {'ok': bool, 'data': ...}。"""
        if not callable(fn):
            raise TypeError(f'handler {cmd!r} 必须可调用')
        self._handlers[str(cmd)] = {'fn': fn, 'desc': desc, 'level': level}
        logger.debug("[nodes] 命令处理器已注册: %s (%s)", cmd, level)

    def unregister_handler(self, cmd: str):
        self._handlers.pop(str(cmd), None)

    def providers(self) -> list:
        return [{'name': k, 'desc': v['desc'], 'level': v['level']}
                for k, v in sorted(self._providers.items())]

    def handlers(self) -> list:
        return [{'cmd': k, 'desc': v['desc'], 'level': v['level']}
                for k, v in sorted(self._handlers.items())]

    # ── 内核自带（协议级语义，与系统环境无关）────────────
    def _register_builtin(self):
        self._providers['core'] = {
            'fn': self._core_info, 'desc': '内核元信息', 'level': 'core'}

        self._handlers['ping'] = {
            'fn': lambda args: {'ok': True, 'data': 'pong'},
            'desc': '连通性探测', 'level': 'core'}
        self._handlers['node.info'] = {
            'fn': lambda args: {'ok': True, 'data': self._core_info()},
            'desc': '节点元信息', 'level': 'core'}
        tq = getattr(self.fw, 'task_queue', None)
        if tq is not None:
            self._handlers['task.list'] = {
                'fn': lambda args: {'ok': True, 'data': {
                    'tasks': tq.list_tasks(state=(args or {}).get('state'),
                                           limit=int((args or {}).get('limit') or 100)),
                    'stats': tq.stats()}},
                'desc': '任务列表', 'level': 'core'}
            self._handlers['task.get'] = {
                'fn': lambda args: self._task_snapshot(
                    tq.get_task(str((args or {}).get('id') or ''))),
                'desc': '任务详情', 'level': 'core'}
            self._handlers['task.cancel'] = {
                'fn': lambda args: self._task_cancel(
                    tq.cancel(str((args or {}).get('id') or ''))),
                'desc': '取消任务（仅排队中）', 'level': 'core'}

    @staticmethod
    def _task_snapshot(task: dict) -> dict:
        if task is None:
            return {'ok': False, 'data': {'error': '任务不存在'}}
        return {'ok': True, 'data': task}

    @staticmethod
    def _task_cancel(result: dict) -> dict:
        if result.get('ok'):
            return {'ok': True, 'data': result}
        return {'ok': False, 'data': str(result.get('error') or '无法取消')}

    @staticmethod
    def _task_snapshot(task: dict) -> dict:
        if task is None:
            return {'ok': False, 'data': {'error': '任务不存在'}}
        return {'ok': True, 'data': task}

    def _core_info(self) -> dict:
        """内核自身元信息（不含任何系统环境数据）。"""
        import time
        fw = self.fw
        info = {
            'version': _fw_version(fw),
            'uptime_seconds': max(0, int(time.time() - getattr(fw, '_start_time', time.time()))),
            'database': getattr(fw.db, 'db_type', 'unknown'),
            'mode': self.mode,
        }
        try:
            info['task_queue'] = fw.task_queue.stats()
        except Exception:
            pass
        try:
            info['extensions'] = list(getattr(fw, '_loaded_extensions', []) or [])
            info['plugins'] = list(getattr(fw, '_loaded_user_plugins', []) or [])
        except Exception:
            pass
        return info

    # ── 数据采集 / 命令执行（agent 与 hub 共用）──────────
    def collect(self) -> dict:
        """收集所有已注册数据源的最新值（单个失败不影响整体）。"""
        out = {}
        for name, p in self._providers.items():
            try:
                out[name] = p['fn']()
            except Exception as e:
                logger.debug("[nodes] 数据源 %s 采集失败: %s", name, e)
                out[name] = {'error': f'{type(e).__name__}: {e}'}
        return out

    def execute(self, cmd: str, args: dict = None) -> dict:
        """按名路由命令到注册者执行；未注册则明确报错（不猜测语义）。"""
        h = self._handlers.get(str(cmd))
        if h is None:
            return {'ok': False,
                    'data': f'节点未注册命令 {cmd!r}（由服务层/软件层注册）'}
        try:
            res = h['fn'](args or {})
            return res if isinstance(res, dict) else {'ok': True, 'data': res}
        except Exception as e:
            logger.exception("[nodes] 命令 %s 执行异常", cmd)
            return {'ok': False, 'data': f'{type(e).__name__}: {e}'}

    # ── 生命周期 ──────────────────────────────────────────
    def start(self):
        if self._started:
            return
        if self.mode == 'off':
            logger.info("[nodes] 多机管理已关闭 (nodes.mode: off)")
            return
        self.registry.ensure_table()
        self._bootstrap()

        if self.mode in ('hub', 'both'):
            hub_cfg = self.cfg.get('hub') or {}
            self.hub = NodeHub(
                self.fw, self.registry,
                host=str(hub_cfg.get('host', '0.0.0.0')),
                port=int(hub_cfg.get('port', 37010)),
                path=str(hub_cfg.get('path', '') or ''),
            )
            self.hub.start()

        if self.mode in ('agent', 'both'):
            agent_cfg = self.cfg.get('agent') or {}
            name = str(agent_cfg.get('name', '') or '').strip()
            secret = str(agent_cfg.get('secret', '') or '').strip()

            # 本机自管（mode=both）零配置：没填节点名 / 密钥就自动生成并入库。
            # 用户在单机上不该被要求手配密钥——节点名默认取主机名，
            # 密钥首次自动生成、之后复用（重启换进程都对得上）。
            if self.mode == 'both' and (not name or not secret):
                name, secret = self._ensure_local_identity(name)

            self.agent = NodeAgent(
                self.fw,
                name=name,
                secret=secret,
                hub_host=str(agent_cfg.get('hub_host', '127.0.0.1')),
                hub_port=int(agent_cfg.get('hub_port', 37010)),
                interval=int(agent_cfg.get('interval', 30)),
                hub_path=str(agent_cfg.get('hub_path', '') or ''),
            )
            self.agent.start()

        self._started = True
        logger.info(f"[nodes] 多机管理已启动，模式 {self.mode}，"
                    f"数据源 {len(self._providers)} 个，"
                    f"命令 {len(self._handlers)} 个，"
                    f"纳管 {len(self.registry.secrets_map())} 个节点")

    def stop(self):
        if self.hub:
            self.hub.stop()
        if self.agent:
            self.agent.stop()
        self._started = False

    # ── 本机身份（零配置）──────────────────────────────────
    def _ensure_local_identity(self, prefer_name: str = '') -> tuple:
        """确保本机节点已纳管并拿到密钥；返回 (name, secret)。

        单机自管（mode=both）时用户不该被要求手配密钥：
        节点名默认取主机名，密钥首次自动生成并入库，之后复用。

        **密钥不明文出现在 config.yaml**——它只存在库里，且通讯实际使用的是
        KDF 派生值（见 `registry.derive_key`）：库里原文即使泄露，也不能直接
        拿来冒充节点，因为派生盐里掺了节点名。
        """
        import platform
        name = str(prefer_name or '').strip() or platform.node() or 'localhost'

        rec = self.registry.get(name)
        if rec and rec.get('secret'):
            return name, rec['secret']          # 已有身份：复用（重启后仍对得上）

        hub_cfg = self.cfg.get('hub') or {}
        res = self.registry.add(
            name, host='127.0.0.1',
            port=int(hub_cfg.get('port', 37010)),
            tags='local')
        if res.get('ok'):
            logger.info("[nodes] 本机节点 %r 已自动纳管，密钥自动生成（无需手动配置）", name)
            return name, res['secret']

        rec = self.registry.get(name)           # 并发兜底：再读一次
        return name, str((rec or {}).get('secret') or '')

    def _bootstrap(self):
        """把 config.yaml 的引导清单导入库（仅补缺，不覆盖既有记录）。"""
        items = (self.cfg.get('registry') or {}).get('bootstrap') or []
        if not isinstance(items, list):
            return
        for i, n in enumerate(items):
            if not isinstance(n, dict):
                continue
            name = str(n.get('name') or f'node-{i + 1}').strip()
            if self.registry.exists(name):
                continue
            if self.registry.add(name, host=n.get('host', ''), port=n.get('port', 0),
                                 tags=n.get('tags', ''),
                                 secret=n.get('secret') or None).get('ok'):
                logger.info(f"[nodes] 已从配置引导纳管节点 {name}")

    # ── 查询 ──────────────────────────────────────────────
    def is_enabled(self) -> bool:
        return self.mode != 'off'

    def local(self) -> dict:
        """本机节点信息（面板用；不走网络）。"""
        import platform
        import time
        fw = self.fw
        return {
            'name': 'localhost', 'online': True, 'is_local': True,
            'platform': f"{platform.system()} {platform.release()}",
            'arch': platform.machine(), 'hostname': platform.node(),
            'python': platform.python_version(),
            'version': _fw_version(fw),
            'uptime_seconds': max(0, int(time.time() - getattr(fw, '_start_time', time.time()))),
        }

    def list(self) -> list:
        """全部节点，合并在线态（控制面实时连接 / 库中快照）。

        一台机器只有一个身份：registry 里已有「从本机连上来的节点」
        （host 为回环地址，即 both 模式的自注册 agent）时，不再合成
        localhost —— 否则面板会看到同一台盒子的两张卡片。
        """
        rows = self.registry.list()
        online = set(self.hub.online_names()) if self.hub else set()
        loopback = {'127.0.0.1', '::1', 'localhost', ''}
        for r in rows:
            # 从本机连上来的节点（both 模式自注册）就是本机，打上标记
            if str(r.get('host') or '').strip() in loopback:
                r['is_local'] = True
            if r['name'] in online:
                r['status'] = 'online'
            elif r.get('status') == 'online':
                r['status'] = 'offline'      # 库中残留 online 但控制面无连接 → 已掉线
        names = {r['name'] for r in rows}
        self_registered = any(str(r.get('host') or '').strip() in loopback for r in rows)
        local = self.local()
        if local['name'] not in names and not self_registered:
            rows.insert(0, {**local, 'status': 'online', 'tags': [], 'last_ok_at': None})
        return rows

    def snapshot(self) -> list:
        """在线节点快照（控制面实时视图）。"""
        return self.hub.snapshot() if self.hub else []

    # ── 纳管 / 吊销 ───────────────────────────────────────
    def add(self, name: str, host: str = '', port: int = 0, tags: str = '',
            secret: str = None) -> dict:
        """纳管节点；返回一次性密钥明文（节点侧据此配置 agent）。"""
        return self.registry.add(name, host=host, port=port, tags=tags, secret=secret)

    def update(self, name: str, **fields) -> dict:
        return self.registry.update(name, **fields)

    def remove(self, name: str) -> dict:
        """吊销纳管：踢下线 + 删库（节点侧需同步清理配置，否则会持续重连）。"""
        if self.hub:
            self.hub.kick(name)
        return self.registry.remove(name)

    def rotate_secret(self, name: str) -> dict:
        """轮换密钥（节点侧需同步更新；旧密钥立即失效）。"""
        if not self.registry.exists(name):
            return {'ok': False, 'error': f'节点 {name} 不存在'}
        new_secret = gen_secret()
        self.registry.update(name, secret=new_secret)
        if self.hub:
            self.hub.kick(name)      # 断开旧连接，迫使节点用新密钥重连
        return {'ok': True, 'name': name, 'secret': new_secret}

    # ── 命令下发 ──────────────────────────────────────────
    def send_cmd(self, name: str, cmd: str, args: dict = None,
                 timeout: float = 30.0) -> dict:
        """向节点下发命令。本机（localhost）直接本地路由，不走网络。"""
        if str(name) in ('localhost', 'local', '本机'):
            return self.execute(cmd, args or {})
        if not self.hub:
            return {'ok': False, 'data': '当前非 hub 模式，无法下发命令'}
        return self.hub.send_cmd(name, cmd, args, timeout)


def _fw_version(fw) -> str:
    try:
        import os
        from core.kernel.paths import project_root
        with open(os.path.join(project_root(), 'VERSION'), encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return ''
