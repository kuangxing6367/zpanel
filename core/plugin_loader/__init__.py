"""
插件加载器核心实现（core.plugin_loader）

原巨型 loader.py（约 2176 行单类，拆分前位于 framework/，该目录已并入 core/）
已按职责拆解为以下细模块：

  - pip.py           pip 镜像安装 + 版本说明符解析（模块级工具函数）
  - source_loader.py 插件源码加载器（始终从 .py 现场编译，不写 __pycache__）
  - base.py          PluginLoaderBase：共享状态（__init__）
  - dependency.py    依赖检查 / 自动安装 / 依赖状态
  - venv.py          隔离虚拟环境管理
  - config.py        插件发现 / 配置文件读写 / 配置 schema
  - module.py        插件模块动态导入（合成包 / 子模块预加载 / 清理）
  - loading.py       单插件加载 / register 调用 / 命令·任务·卡片同步 / 心跳·自检
  - lifecycle.py     卸载 / 已加载查询
  - webui.py         仪表盘卡片 / WebUI 扩展 / 前端接管
  - group.py         群级插件开关

PluginLoader 由上述 mixin 线性组合而成，对外 API（方法名、签名、行为）
与拆分前完全一致；下面对外暴露的即为原类，引用方零改动。

注：service/zkg/loader.py（Loader 类，服务级包管理器）是依赖驱动的加载器，
与本包职责不同，故插件加载器落在 core.plugin_loader 包以避名冲突。
"""
from .base import PluginLoaderBase
from .dependency import DependencyMixin
from .venv import VenvMixin
from .config import ConfigMixin
from .module import ModuleMixin
from .loading import LoadingMixin
from .lifecycle import LifecycleMixin
from .webui import WebUiMixin
from .group import GroupMixin
from .source_loader import _PluginSourceLoader
from .pip import (
    pip_install_with_mirror,
    pip_install_all,
    pip_install_requirements,
    _parse_version_spec,
    _parse_ver,
    _check_version_compatible,
    _parse_requirements_file,
    _PIP_MIRRORS,
    _RE_PKG_NAME,
    _RE_SIMPLE_SPEC,
)
from .config import (
    _CONFIG_FILE_EXTS,
    _CONFIG_FILE_NAMES,
    _CODE_FILE_NAMES,
)


class PluginLoader(
    PluginLoaderBase,
    DependencyMixin,
    VenvMixin,
    ConfigMixin,
    ModuleMixin,
    LoadingMixin,
    LifecycleMixin,
    WebUiMixin,
    GroupMixin,
):
    """插件加载器，管理插件生命周期（由细粒度 mixin 组合而成）。"""


__all__ = [
    'PluginLoader',
    '_PluginSourceLoader',
    'pip_install_with_mirror',
    'pip_install_all',
    'pip_install_requirements',
    '_parse_version_spec',
    '_parse_ver',
    '_check_version_compatible',
    '_parse_requirements_file',
    '_PIP_MIRRORS',
    '_RE_PKG_NAME',
    '_RE_SIMPLE_SPEC',
    '_CONFIG_FILE_EXTS',
    '_CONFIG_FILE_NAMES',
    '_CODE_FILE_NAMES',
]
