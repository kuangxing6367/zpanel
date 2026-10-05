"""
PluginContext 基础层（core/ctx 节点 2）

持有全部共享状态与最基础的属性/便捷能力：
- __init__：初始化 framework / db 引用与各注册集合（命令、任务、卡片、扩展、处理器）
- onebot / logger / plugin_name / _current_bot 属性
- get_data_dir：返回插件数据目录（plugins_dat/<name>/）
- log / run_async：通用日志与线程池提交

所有 mixin 均假定这些属性已由本基类在 __init__ 中建立。
"""

import os

from .logger import PluginLogger, _async_executor


class PluginContextBase:
    """插件上下文基类：共享状态与基础属性（不含任何注册/业务方法）"""

    def __init__(self, plugin_name: str, framework):
        self._plugin_name = plugin_name
        self._framework = framework  # 框架引擎引用
        self._db = framework.db
        self._commands = []  # 本次注册周期收集的命令
        self._tasks = []     # 本次注册周期收集的任务
        self._dashboard_cards = []  # 仪表盘卡片
        self._raw_message_handlers = []  # 原始消息处理器（收到原始消息事件，可选择性接管）
        self._group_extensions = []  # WebUI 群组管理页插件扩展
        self._user_extensions = []   # WebUI 用户管理页插件扩展
        self._logger = PluginLogger(plugin_name)
        self._config_cache = {}      # key -> (value, timestamp) 插件配置 TTL 缓存
        self._config_cache_ttl = 30  # 缓存有效期（秒），避免 async handler 同步查库阻塞事件循环

    @property
    def onebot(self):
        """
        OneBot 11 API 封装（兼容旧插件的便捷面）。
        优先返回当前接入端注册的专用动作封装 services['onebot_api']；
        若接入端只注册了通用 api_caller，则用协议无关的 ActionProxy 兜底，
        框架核心本身不包含任何 OneBot 实现。
        """
        api = self._framework.services.get('onebot_api')
        if api is not None:
            return api
        caller = self._framework.services.get('api_caller')
        if caller is not None:
            from core.adapters.protocol import ActionProxy
            return ActionProxy(caller)
        raise RuntimeError("无可用协议适配器（请启用一个接入端，如 extensions.onebot_adapter 或 extensions.http_inject）")

    @property
    def logger(self):
        """获取插件日志记录器（标准 logger 接口）"""
        return self._logger

    @property
    def plugin_name(self) -> str:
        """获取当前插件名"""
        return self._plugin_name

    @property
    def api_version(self) -> int:
        """插件 API 版本（稳定 ABI 承诺；见 core/ctx.PLUGIN_API_VERSION）"""
        from core.ctx import PLUGIN_API_VERSION
        return PLUGIN_API_VERSION

    @property
    def command_bus(self):
        """内核命令总线（薄分发原语 register/invoke），供插件直接注册运行时命令。

        注意：这与 ``command()`` 是两套东西——
        - ``command()`` 收集「声明式命令」（pattern/优先级/权限等），落库并由扩展层 router 路由；
        - ``command_bus`` 是内核级 name→callable 运行时分发原语，不含任何路由/匹配策略。
        """
        return self._framework.command_bus

    def zkg_tool(self, name: str):
        """获取 zkg 按需加载的官方机制包模块（service/zkg），未加载返回 None。

        机制包由 zkg 依据插件 manifest.toml 的 ``dependencies`` 声明按需加载
        （如 ``store`` / ``exec`` / ``ws``），启动后在框架实例上统一暴露。
        在 manifest 声明依赖 + 这里取用，是插件消费机制包的标准路径。
        """
        return getattr(self._framework, 'zkg_tools', {}).get(name)

    @property
    def _current_bot(self):
        """
        当前消息来源的 OneBot 实例名（由框架在 handler 执行期间通过 contextvars 注入）
        多 bot 并发场景下每个事件独立，不再使用插件级共享变量（原 router 直接写
        module.ctx._current_bot 会在并发消息交错时发错 bot）
        """
        try:
            from core.runtime import current_source_var
            return current_source_var.get()
        except Exception:
            return None

    def get_data_dir(self) -> str:
        """
        获取当前插件的数据/配置目录绝对路径（plugins_dat/<plugin_name>/）
        插件应在此目录下读写自己的配置文件、缓存数据等，而非 plugins/ 代码目录
        """
        dat_dir = os.path.join(
            self._framework.plugin_loader.plugins_dat_dir,
            self._plugin_name
        )
        if not os.path.isdir(dat_dir):
            os.makedirs(dat_dir, exist_ok=True)
        return dat_dir

    def log(self, msg: str, level: str = 'info'):
        """输出日志"""
        level = level.upper()
        log_method = getattr(__import__('logging').getLogger('zernus'), level.lower(), __import__('logging').getLogger('zernus').info)
        log_method(f"[{self._plugin_name}] {msg}")

    def run_async(self, func, *args, **kwargs):
        """
        异步执行耗时操作（如图片渲染、网络请求），不阻塞主消息处理流程。

        提交的任务在线程池中执行，返回 concurrent.futures.Future 对象。
        适合用于图片渲染、文件处理等不需要立即返回的耗时操作。
        """
        return _async_executor.submit(func, *args, **kwargs)
