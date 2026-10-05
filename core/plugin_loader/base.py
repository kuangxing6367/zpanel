"""
PluginLoader 共享状态（__init__）

所有实例属性在此集中定义，mixin 仅消费这些属性；保证任意 mixin 方法在
未显式定义属性时也能从基类拿到正确状态。
"""
import os
import threading
from typing import Dict


class PluginLoaderBase:
    """插件加载器共享状态基类（不含任何业务逻辑方法）。"""

    def __init__(self, plugins_dir: str, framework, plugins_dat_dir: str = None):
        self.plugins_dir = plugins_dir
        self.plugins_dat_dir = plugins_dat_dir or os.path.join(
            os.path.dirname(plugins_dir.rstrip(os.sep)), 'data', 'plugins_dat'
        )
        self.framework = framework
        self.db = framework.db
        self._loaded_plugins: Dict[str, dict] = {}  # plugin_name -> {module, register_func, ...}
        self._lock = threading.Lock()
        self._override_webui: str = None  # 接管整个前端的插件名（None 表示用框架默认前端）
        self._missing_deps: Dict[str, list] = {}   # plugin_name -> [缺失的包列表]
        self._conflict_deps: Dict[str, list] = {}  # plugin_name -> [{name, required, installed}, ...]
        self._isolated_plugins: set = set()         # plugin_name -> 已启用隔离环境的插件
        self._memory_monitor_running = False
        self._memory_violations: Dict[str, int] = {}  # plugin_name -> 连续超限次数

        # ── 群级插件开关缓存 {group_id: {plugin_name: enabled}} ──
        self._group_plugin_cache = {}
        self._group_plugin_cache_time = 0
        self._group_cache_ttl = 30  # 缓存 30 秒

        # ── 插件文件 mtime 快照（心跳增量注册用）──
        self._plugin_mtimes: Dict[str, float] = {}
