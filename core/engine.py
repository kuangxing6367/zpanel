"""
框架核心引擎（装配层）

本文件只负责「组装」：把内核原语（core.kernel）与运行时节点（core.runtime）
按顺序装配成可运行引擎；并对旧调用点保留向后兼容的薄委托方法。

单一职责实现已拆到：
- core.kernel.*   最小原语（event_bus / banner / logging_setup / data_dirs /
                  stats_writer / paths）
- core.runtime.*  编排节点（dispatch / plugins / watchdogs / lifecycle / reply）
"""
import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from core.config import load_config
from core.storage import init_db
from core.plugin_loader import PluginLoader
from core.messaging.router import MessageRouter
from core.messaging.event_bus import EventBus
from core.log_broker import log_broker, FrameworkLogHandler
from core.adapters.protocol import ServiceRegistry
from core.terminal import TerminalInput, terminal_commands
from core.hooks import HookRegistry

from core.kernel.banner import read_version, emit_banner
from core.kernel.logging_setup import setup_logging
from core.kernel.data_dirs import migrate_legacy_data_dirs
from core.kernel.stats_writer import AsyncStatsWriter
from core.commands import CommandBus
from core import runtime

logger = logging.getLogger('zernus')


class Framework:
    """框架核心引擎（装配层）"""

    def __init__(self, config_path: str = None):
        # 记录实际使用的配置文件路径（供 Web API 读写 config.yaml 使用）
        if config_path is None:
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config.yaml')
        self.config_path = os.path.abspath(config_path)
        self.config = load_config(config_path)
        # 数据目录统一迁移（logs / plugins_dat → data/ 下），必须在日志与插件加载前执行
        self._migrate_legacy_data_dirs()
        self._setup_logging()

        logger.info("正在初始化框架核心引擎...")

        # 服务注册表（官方插件注册自身为核心能力）
        self.services = ServiceRegistry(self)

        # 扩展点注册表
        self.hooks = HookRegistry(self)

        # 数据库：真实数据库（SQLite / MySQL / PostgreSQL 三方言，由 config.database.type 决定）
        self.db = init_db(self.config['database'])
        # 把扩展点注册表注入存储引擎，使 db.query/execute 能触发 db.* hook
        self.db._hooks = self.hooks

        # 数据库专用线程池
        self._db_executor = ThreadPoolExecutor(
            max_workers=max(8, min(32, (os.cpu_count() or 4) * 2)),
            thread_name_prefix='zcdb',
        )

        self.event_bus = EventBus()
        self.command_bus = CommandBus()
        self.router = MessageRouter(self)
        self.plugin_loader = PluginLoader(
            self._get_plugins_dir(), self, self._get_plugins_dat_dir())
        self.terminal = TerminalInput(self)
        self.stats_writer = AsyncStatsWriter(self.db, self._db_executor)

        # 内核任务队列（任务列表；core/service/software 均可提交后台任务）
        from core.kernel.task_queue import TaskQueue
        tq_cfg = self.config.get('task_queue') or {}
        self.task_queue = TaskQueue(
            workers=tq_cfg.get('workers', 4),
            max_history=tq_cfg.get('max_history', 200),
        )

        # zkg 按需加载的机制包（service/startup 启动时填充；插件经 ctx.zkg_tool 取用）
        self.zkg_tools = {}

        # 原始消息处理器注册表
        self._raw_message_handlers = []
        # 后台事件任务引用集
        self._pending_tasks = set()
        # 启动横幅所需的已加载清单
        self._loaded_extensions = []
        self._loaded_user_plugins = []

        # Web API 路由注册表由 webui 扩展在启动时注入（fw.api_registry）；
        # 内核只持中立缓冲，绝不直接依赖 Web 包（保持层倒置为 0）。
        self.api_registry = None
        self._pending_api_routes = []

        # 心跳参数
        self._heartbeat_interval = self.config['plugin'].get('heartbeat_interval', 60)
        self._heartbeat_task = None
        self._running = False
        self.loop = None

        # 内存看门狗参数（统一取自 service.watchdog，与服务级看门狗同一来源）
        wd_cfg = ((self.config.get('service') or {}).get('watchdog') or {})
        self._memory_limit_mb = int(wd_cfg.get('max_memory_mb', 256))
        self._memory_check_interval = int(wd_cfg.get('interval', 30))
        self._memory_watchdog_task = None

        import time
        self._start_time = time.time()

        # 内核级多机管理：节点注册表 / 控制面 / 代理 / 数据与命令路由。
        # 内核在此只持有「路由能力」，不关心任何系统环境；
        # 数据源与命令处理器由服务层 / 软件层在启动时注册进来。
        from core.nodes import NodeManager
        self.nodes = NodeManager(self)

        logger.info("框架核心引擎初始化完成")

    # ── 服务别名（兼容旧代码，指向 service registry）──

    @property
    def api_caller(self):
        return self.services.get('api_caller')

    @property
    def ws_server(self):
        return self.services.get('ws_server')

    @property
    def scheduler(self):
        return self.services.get('scheduler')

    @property
    def web_server(self):
        return self.services.get('web_server')

    # ── 路径 / 初始化（委托 core.kernel）──

    def _migrate_legacy_data_dirs(self):
        """数据目录统一迁移（委托 core.kernel.data_dirs）。"""
        migrate_legacy_data_dirs(os.path.dirname(os.path.dirname(__file__)))

    def _setup_logging(self):
        """配置日志（委托 core.kernel.logging_setup；LogBroker 桥接由框架侧注入）。"""
        project_root = os.path.dirname(os.path.dirname(__file__))
        self._log_file = setup_logging(
            self.config, project_root,
            extra_handlers=[FrameworkLogHandler(log_broker)],
        )

    def _get_plugins_dir(self) -> str:
        """获取插件代码目录路径（优先使用配置）"""
        plugin_dir = self.config.get('plugin', {}).get('dir', '')
        if plugin_dir:
            if os.path.isabs(plugin_dir):
                return plugin_dir
            return os.path.join(os.path.dirname(os.path.dirname(__file__)), plugin_dir)
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), 'software', 'plugins')

    def _get_plugins_dat_dir(self) -> str:
        """获取插件数据/配置目录路径"""
        dat_dir = self.config.get('plugin', {}).get('dat_dir', '')
        if dat_dir:
            if os.path.isabs(dat_dir):
                return dat_dir
            return os.path.join(os.path.dirname(os.path.dirname(__file__)), dat_dir)
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'plugins_dat')

    # ── 生命周期（委托 core.runtime.lifecycle）──

    async def start(self):
        """启动引擎（异步）"""
        await runtime.start(self)

    async def stop(self):
        """停止引擎（异步）"""
        await runtime.stop(self)

    def _read_version(self) -> str:
        """读取 VERSION 文件（委托 core.kernel.banner）。"""
        return read_version(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def _print_startup_banner(self):
        """启动横幅（委托 core.kernel.banner）。"""
        emit_banner(
            version=self._read_version(),
            project_root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            core_loaded=getattr(self, '_loaded_extensions', []),
            user_loaded=getattr(self, '_loaded_user_plugins', []),
            config=self.config,
            logger=logger,
        )

    def _format_uptime(self):
        """格式化运行时间（委托 core.runtime.lifecycle）"""
        return runtime.format_uptime(self)

    def _warn_insecure_config(self):
        runtime.warn_insecure_config(self)

    def _auto_heal_plugin_deps(self):
        runtime.auto_heal_plugin_deps(self)

    async def _heartbeat_loop(self):
        await runtime.heartbeat_loop(self)

    async def _memory_watchdog_loop(self):
        await runtime.memory_watchdog_loop(self)

    # ── 官方插件（委托 core.runtime.plugins）──

    def _load_extensions(self):
        runtime.load_extensions(self)

    # ── 事件分发（委托 core.runtime.dispatch）──

    async def dispatch_event(self, event: dict):
        await runtime.dispatch_event(self, event)

    def register_raw_message_handler(self, plugin_name: str, handler, priority: int = 50):
        runtime.register_raw_message_handler(self, plugin_name, handler, priority)

    def unregister_raw_message_handlers(self, plugin_name: str):
        runtime.unregister_raw_message_handlers(self, plugin_name)

    async def _dispatch_raw_message_handlers(self, data: dict, bot_name: str) -> bool:
        return await runtime.dispatch_raw_message_handlers(self, data, bot_name)

    async def _handle_notice(self, data: dict, bot_name: str = 'default'):
        await runtime.handle_notice(self, data, bot_name)

    async def _handle_request(self, data: dict, bot_name: str = 'default'):
        await runtime.handle_request(self, data, bot_name)

    def _sync_group_member_join(self, data: dict):
        runtime.sync_group_member_join(self, data)

    def _sync_group_member_leave(self, data: dict):
        runtime.sync_group_member_leave(self, data)

    # ── 回复 / 生命周期钩子（委托 core.runtime.reply）──

    async def reply_text(self, target, text: str):
        return await runtime.reply_text(self, target, text)

    def _on_message_sent(self, bot_name: str, action: str, params: dict, resp: dict):
        runtime.on_message_sent(self, bot_name, action, params, resp)

    # ── 内置任务 / 终端 / SSL（保留在本层）──

    def _register_builtin_jobs(self):
        """注册框架内置定时任务（与插件任务互不干扰）"""
        try:
            from core import perm as perm_mod

            def _cleanup_expired_perms():
                try:
                    perm_mod.cleanup_expired(self.db)
                except Exception as e:
                    logger.warning(f"权限过期清理任务异常: {e}")

            sched = self.scheduler
            if sched is None:
                return
            sched.add_plugin_task({
                'plugin_name': 'builtin',
                'id': 'builtin_perm_cleanup',
                'cron_expression': '17 * * * *',
                'handler': _cleanup_expired_perms,
                'handler_name': '_cleanup_expired_perms',
                'description': '权限过期清理',
            })
            logger.debug("内置定时任务已注册: 权限过期清理（每小时第 17 分钟）")
        except Exception as e:
            logger.warning(f"注册内置定时任务失败: {e}")

    async def terminal_exec(self, name: str, args: str = '') -> str:
        """执行一条终端命令并捕获其输出。"""
        import io
        import contextlib
        handler = terminal_commands.get(name)
        if handler is None:
            return f"未知命令: {name}"
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                if asyncio.iscoroutinefunction(handler):
                    await handler(args)
                else:
                    await asyncio.to_thread(handler, args)
        except Exception as e:
            return f"命令 [{name}] 执行失败: {e}"
        out = buf.getvalue()
        return out if out.strip() else f"[{name}] 已执行"

    def build_ssl_context(self):
        """构建服务端 SSLContext（Web HTTPS 与 OneBot WSS 共用 config['ssl']）。"""
        from core.tls import build_server_ssl_context
        return build_server_ssl_context(
            self.config.get('ssl', {}), os.path.dirname(self.config_path))
