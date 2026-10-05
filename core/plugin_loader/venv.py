"""
隔离虚拟环境管理 mixin

为插件创建/删除/扫描独立的 venv（plugins_dat/<插件名>/.venv），与代码分离；
并提供将 venv site-packages 注入 sys.path 的能力。
"""
import glob as _glob
import logging
import os
import shutil
import subprocess
import sys

from .pip import pip_install_with_mirror

logger = logging.getLogger('zernus')


class VenvMixin:
    """插件隔离虚拟环境操作。"""

    def _venv_dir(self, plugin_name: str) -> str:
        """插件虚拟环境目录（统一存放于 plugins_dat/<插件名>/.venv，与代码分离）"""
        return os.path.join(self._plugin_dat_dir(plugin_name), '.venv')

    def create_isolated_env(self, plugin_name: str) -> dict:
        """
        为插件创建隔离虚拟环境（手动触发，Web UI 点击「创建虚拟环境」调用）
        venv 创建在 plugins_dat/<插件名>/.venv，与插件代码目录分离。
        """
        # 确保 plugins_dat/<插件名> 目录存在
        self.ensure_plugins_dat_dir(plugin_name)
        venv_path = self._venv_dir(plugin_name)

        # 获取插件所有依赖（合并所有声明来源）
        deps = self._get_merged_dependencies(plugin_name)

        try:
            # 1. 创建虚拟环境
            logger.info(f"[{plugin_name}] 正在创建虚拟环境: {venv_path}")
            subprocess.check_call(
                [sys.executable, '-m', 'venv', venv_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )

            # 2. 安装依赖
            return self.install_deps_to_venv(plugin_name, deps, venv_path)

        except subprocess.TimeoutExpired:
            return {'success': False, 'error': '创建 venv 超时（60s）'}
        except Exception as e:
            logger.error(f"[{plugin_name}] 创建隔离环境失败: {e}")
            return {'success': False, 'error': str(e)}

    def install_deps_to_venv(self, plugin_name: str, deps: list,
                             venv_path: str = None) -> dict:
        """
        将依赖安装到插件的隔离虚拟环境中
        :param plugin_name: 插件名
        :param deps: 依赖列表
        :param venv_path: venv 路径，None 则使用 plugins_dat/<插件名>/.venv
        :return: {'success': bool, 'venv_path': str, 'python': str, 'installed': list, 'failed': list}
        """
        if venv_path is None:
            venv_path = self._venv_dir(plugin_name)

        if not os.path.isdir(venv_path):
            return {'success': False, 'error': f'venv 不存在: {venv_path}'}

        # 获取 venv 内的 pip/python
        if sys.platform == 'win32':
            pip_path = os.path.join(venv_path, 'Scripts', 'pip.exe')
            python_path = os.path.join(venv_path, 'Scripts', 'python.exe')
        else:
            pip_path = os.path.join(venv_path, 'bin', 'pip')
            python_path = os.path.join(venv_path, 'bin', 'python')

        if not os.path.isfile(python_path):
            return {'success': False, 'error': 'venv 中未找到 python'}

        # 安装依赖
        installed = []
        failed = []
        for dep in deps:
            try:
                logger.info(f"[{plugin_name}] 隔离环境安装依赖: {dep}")
                # 注意：pip_install_with_mirror 使用 {exec} -m pip install 模式，
                # 所以必须传 venv 的 python 路径，而不是 pip 路径（否则会变成 pip -m pip install 这样的错误命令）
                r = pip_install_with_mirror(python_path, dep, timeout=120)
                if r['success']:
                    installed.append(dep)
                else:
                    failed.append(dep)
                    logger.warning(f"[{plugin_name}] 隔离环境安装依赖失败: {dep} - {r.get('error')}")
            except Exception as e:
                failed.append(dep)
                logger.warning(f"[{plugin_name}] 隔离环境安装依赖失败: {dep} - {e}")

        success = len(failed) == 0
        if success:
            with self._lock:
                self._isolated_plugins.add(plugin_name)
                self._conflict_deps.pop(plugin_name, None)
                if not self._missing_deps.get(plugin_name):
                    self._missing_deps.pop(plugin_name, None)
            logger.info(f"[{plugin_name}] 隔离环境安装完成: {venv_path}")

        return {
            'success': success,
            'venv_path': venv_path,
            'python': python_path,
            'installed': installed,
            'failed': failed,
        }

    def remove_isolated_env(self, plugin_name: str) -> dict:
        """删除插件的隔离虚拟环境（plugins_dat/<插件名>/.venv）"""
        venv_path = self._venv_dir(plugin_name)
        if not os.path.isdir(venv_path):
            return {'success': True, 'msg': '无隔离环境'}
        try:
            shutil.rmtree(venv_path, ignore_errors=True)
            with self._lock:
                self._isolated_plugins.discard(plugin_name)
            logger.info(f"[{plugin_name}] 隔离环境已删除: {venv_path}")
            return {'success': True, 'msg': '隔离环境已删除'}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def scan_venv_usage(self) -> dict:
        """
        扫描所有插件的 .venv 隔离环境（plugins_dat/<插件名>/.venv），返回磁盘占用信息
        用于运维监控，防止香橙派等低磁盘设备空间被虚拟环境耗尽
        """
        result = {
            'total_size_mb': 0,
            'venv_count': 0,
            'details': [],
        }
        if not os.path.isdir(self.plugins_dat_dir):
            return result

        for name in os.listdir(self.plugins_dat_dir):
            venv_path = os.path.join(self.plugins_dat_dir, name, '.venv')
            if not os.path.isdir(venv_path):
                continue

            try:
                size_bytes = 0
                for root, dirs, files in os.walk(venv_path):
                    for f in files:
                        fp = os.path.join(root, f)
                        try:
                            size_bytes += os.path.getsize(fp)
                        except OSError:
                            pass
                size_mb = round(size_bytes / 1024 / 1024, 1)
                result['total_size_mb'] += size_mb
                result['venv_count'] += 1
                result['details'].append({
                    'plugin_name': name,
                    'venv_path': venv_path,
                    'size_mb': size_mb,
                })
            except Exception as e:
                logger.warning(f"扫描 .venv 失败 [{name}]: {e}")

        result['total_size_mb'] = round(result['total_size_mb'], 1)
        return result

    def _add_venv_to_path(self, plugin_name: str) -> bool:
        """
        将插件的 venv site-packages 加入 sys.path，使其依赖在主进程中可见。
        venv 位于 plugins_dat/<插件名>/.venv。
        返回 True 表示 venv 可用，False 表示 venv 不可用。
        """
        venv_path = self._venv_dir(plugin_name)
        if not os.path.isdir(venv_path):
            return False

        # 计算 site-packages 路径
        if sys.platform == 'win32':
            site_pkg = os.path.join(venv_path, 'Lib', 'site-packages')
        else:
            # 先找 python3.x/site-packages
            python_dirs = _glob.glob(os.path.join(venv_path, 'lib', 'python*'))
            site_pkg = os.path.join(python_dirs[0], 'site-packages') if python_dirs else None

        if not site_pkg or not os.path.isdir(site_pkg):
            return False

        if site_pkg not in sys.path:
            sys.path.insert(0, site_pkg)
        return True
