"""
插件模块动态导入 mixin

实现「合成包 + 三层模块名」机制：把插件目录加载为合法 Python 包，
主模块与子模块每次都从 .py 源码现场编译，保证相对导入 / 短名绝对导入 /
嵌套包多级相对导入均可用，并支持热重载时干净清理 sys.modules。
"""
import importlib
import importlib.util
import logging
import os
import shutil
import sys
import types

from .source_loader import _PluginSourceLoader

logger = logging.getLogger('zernus')


class ModuleMixin:
    """插件模块装载与清理。"""

    def _ensure_plugin_package(self, plugin_name: str, plugin_path: str):
        """
        创建插件的「合成包」模块 plugin_<插件名> 并返回。

        框架不把插件目录作为常规包安装，而是用 importlib 按文件路径加载，
        这个模块身兼两职：
        1. main.py 的执行载体 —— 保持 sys.modules['plugin_<插件名>'] 指向插件主模块
           这一既有约定（跨插件可用 sys.modules.get('plugin_xxx') 访问主模块）；
        2. 相对导入的父包 —— 通过设置 __package__ 与 __path__，让导入系统把它识别为包，
           main.py 及子模块中的相对导入（from .xxx import Y / from . import xxx）
           就能沿着 __path__（即插件目录）解析到本插件自己的模块。

        必须在预加载任何子模块之前创建：否则子模块执行相对导入时会找不到父包。
        """
        pkg_name = f"plugin_{plugin_name}"
        main_file = os.path.join(plugin_path, 'main.py')
        module = types.ModuleType(pkg_name)
        module.__name__ = pkg_name
        # 包的 __package__ 指向自身；main.py 里的 from .xxx 以它为父包
        module.__package__ = pkg_name
        # 关键：__path__ 让导入系统把该模块当作包，按插件目录查找子模块
        module.__path__ = [plugin_path]
        module.__file__ = main_file
        sys.modules[pkg_name] = module
        return module

    def _load_plugin_submodule(self, plugin_name: str, mod_name: str, file_path: str):
        """
        加载插件的一个顶层子模块（.py 文件，或包目录的 __init__.py），
        并在 sys.modules 中为「同一个模块对象」注册三个名字：

        1. plugin_<插件名>.<模块名> —— 规范的点分层级名，挂在合成包下，
           插件内相对导入（from .mod import X / from . import mod）解析到它；
        2. plugin_<插件名>_<模块名> —— 旧版下划线唯一名（向后兼容，
           同时保证多个插件存在同名子模块时互不冲突）；
        3. <模块名> —— 短名，兼容 main.py 的绝对导入（import mod / from mod import X）。

        调用前必须已通过 _ensure_plugin_package() 创建父包 plugin_<插件名>。
        """
        pkg_name = f"plugin_{plugin_name}"
        dotted_name = f"{pkg_name}.{mod_name}"
        legacy_name = f"{pkg_name}_{mod_name}"
        try:
            spec = importlib.util.spec_from_file_location(
                dotted_name, file_path,
                loader=_PluginSourceLoader(dotted_name, file_path))
            if spec is None or spec.loader is None:
                return
            module = importlib.util.module_from_spec(spec)
            # 先登记再执行：模块执行期间触发的导入即可命中本插件自身
            sys.modules[dotted_name] = module
            sys.modules[legacy_name] = module
            spec.loader.exec_module(module)
            # 短名覆盖：main.py 的 'import mod' / 'from mod import X' 在导入时绑定，
            # 后续其他插件覆盖短名不影响本插件已绑定的引用
            sys.modules[mod_name] = module
        except Exception as e:
            # 回滚半初始化登记，避免挡住原生导入机制沿 __path__ 的兜底解析
            sys.modules.pop(dotted_name, None)
            sys.modules.pop(legacy_name, None)
            logger.warning(f"[{plugin_name}] 子模块 {mod_name} 预加载失败（回退到全局查找）: {e}")

    def _preload_plugin_submodules(self, plugin_name: str, plugin_path: str):
        """
        预加载插件目录下的顶层子模块（.py 文件与包目录），实现同名模块短名隔离。
        解决多个插件存在同名模块（如 ban_word.py / db.py / core/）时，
        后加载插件从 sys.modules 命中其他插件模块导致的 ImportError。

        即使此处遗漏或预加载失败，合成包的 __path__ 仍会让 Python 原生导入机制
        按插件目录兜底解析，因此本方法只负责「提前、正确地登记」。
        """
        try:
            entries = os.listdir(plugin_path)
        except OSError:
            return

        # 先加载顶层 .py 模块，再加载包目录（包内可能绝对导入顶层模块）
        py_files = []
        pkg_dirs = []
        for fname in entries:
            fpath = os.path.join(plugin_path, fname)
            if (os.path.isfile(fpath) and fname.endswith('.py')
                    and fname not in ('main.py', '__init__.py')):
                py_files.append((fname[:-3], fpath))
            elif (os.path.isdir(fpath)
                    and os.path.isfile(os.path.join(fpath, '__init__.py'))):
                pkg_dirs.append((fname, os.path.join(fpath, '__init__.py')))

        for mod_name, fpath in py_files:
            self._load_plugin_submodule(plugin_name, mod_name, fpath)
        for mod_name, fpath in pkg_dirs:
            self._load_plugin_submodule(plugin_name, mod_name, fpath)

    def _purge_plugin_modules(self, plugin_name: str, plugin_path: str = None):
        """
        从 sys.modules 移除属于某插件目录的全部模块。
        点分层级名 / 下划线唯一名 / 短名指向同一模块对象（__file__ 相同），
        按 __file__ 前缀扫描即可一次清净；最后兜底移除合成包主模块。
        卸载、加载失败回滚、重试前清理共用此方法。
        """
        plugin_path = plugin_path or os.path.join(self.plugins_dir, plugin_name)
        try:
            abs_plugin = os.path.abspath(plugin_path)
            for mod_name in list(sys.modules):
                mod = sys.modules.get(mod_name)
                mod_file = getattr(mod, '__file__', '') or ''
                if mod_file and os.path.abspath(mod_file).startswith(abs_plugin + os.sep):
                    sys.modules.pop(mod_name, None)
            sys.modules.pop(f"plugin_{plugin_name}", None)
        except Exception as e:
            logger.debug(f"[{plugin_name}] 清理 sys.modules 异常: {e}")

    def _clear_plugin_bytecode_cache(self, plugin_path: str):
        """
        删除插件目录下的 __pycache__，保证（完全）重载时总是从源码重新编译。

        CPython 依据“源码整数秒 mtime + 文件大小”校验 .pyc：若在同一秒内把
        文件改成相同字节数（自动化改码/快速热重载场景），会误判字节码仍有效而
        复用旧代码。插件加载并不频繁，直接清掉本插件的字节码缓存最稳妥；
        只删除本插件目录内的 __pycache__，不影响其他插件。
        """
        try:
            for root, dirs, _files in os.walk(plugin_path):
                if "__pycache__" in dirs:
                    shutil.rmtree(os.path.join(root, "__pycache__"), ignore_errors=True)
            importlib.invalidate_caches()
        except Exception as e:
            logger.debug(f"清理插件字节码缓存异常: {e}")
