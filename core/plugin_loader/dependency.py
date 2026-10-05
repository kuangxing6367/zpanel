"""
依赖检查 / 自动安装 / 依赖状态 mixin

合并 requirements.txt 与 plugin.yaml 多处依赖声明，检查缺失与版本冲突，
并在需要时自动安装到当前环境（或隔离虚拟环境由 venv mixin 处理）。
"""
import importlib.metadata
import logging
import os
import sys

import yaml

from .pip import (
    _parse_requirements_file,
    _parse_version_spec,
    _check_version_compatible,
    pip_install_with_mirror,
)

logger = logging.getLogger('zernus')


class DependencyMixin:
    """依赖声明合并、检查与自动安装。"""

    def _read_requirements_txt(self, plugin_name: str) -> list:
        """
        读取插件代码目录下的 requirements.txt
        位置: {plugins_dir}/{plugin_name}/requirements.txt
        返回依赖列表（去重）
        """
        req_file = os.path.join(self.plugins_dir, plugin_name, 'requirements.txt')
        return _parse_requirements_file(req_file)

    def _get_merged_dependencies(self, plugin_name: str) -> list:
        """
        合并插件的所有依赖声明来源，返回去重后的依赖列表（保留顺序）
        合并顺序（优先级从低到高，同名以后续的版本约束为准）：
          1. plugins/<name>/requirements.txt           — 插件代码目录中的依赖文件
          2. plugins_dat/<name>/plugin.yaml → deps.python  — 配置目录的 yaml 声明
          3. plugins/<name>/plugin.yaml → deps.python      — 代码目录 yaml（作为 fallback）
        """
        merged = []
        seen_pkg = {}  # pkg_name(lower) → index in merged for overwrite

        # 1. 从代码目录 requirements.txt 读取
        for dep in self._read_requirements_txt(plugin_name):
            pkg, _, _ = _parse_version_spec(dep)
            key = pkg.lower()
            if key in seen_pkg:
                merged[seen_pkg[key]] = dep  # 覆盖为后续的版本约束
            else:
                seen_pkg[key] = len(merged)
                merged.append(dep)

        # 2. 从 plugins_dat 下的 plugin.yaml 读取
        yaml_dat = self.read_plugin_yaml(plugin_name)
        yaml_deps = yaml_dat.get('dependencies', {}).get('python', []) if isinstance(yaml_dat, dict) else []
        for dep in yaml_deps:
            if not isinstance(dep, str):
                continue
            dep = dep.strip()
            if not dep:
                continue
            pkg, _, _ = _parse_version_spec(dep)
            key = pkg.lower()
            if key in seen_pkg:
                merged[seen_pkg[key]] = dep
            else:
                seen_pkg[key] = len(merged)
                merged.append(dep)

        # 3. 从代码目录下的 plugin.yaml 读取（fallback，防止首次加载时 plugins_dat 没有 yaml）
        code_yaml_path = os.path.join(self.plugins_dir, plugin_name, 'plugin.yaml')
        if os.path.isfile(code_yaml_path):
            try:
                with open(code_yaml_path, 'r', encoding='utf-8') as f:
                    code_yaml = yaml.safe_load(f) or {}
                code_deps = code_yaml.get('dependencies', {}).get('python', []) if isinstance(code_yaml, dict) else []
                for dep in code_deps:
                    if not isinstance(dep, str):
                        continue
                    dep = dep.strip()
                    if not dep:
                        continue
                    pkg, _, _ = _parse_version_spec(dep)
                    key = pkg.lower()
                    if key in seen_pkg:
                        merged[seen_pkg[key]] = dep
                    else:
                        seen_pkg[key] = len(merged)
                        merged.append(dep)
            except Exception as e:
                logger.warning(f"[{plugin_name}] 读取代码目录 plugin.yaml 失败: {e}")

        return merged

    def check_dependencies(self, plugin_name: str) -> dict:
        """
        检查插件的 Python 依赖是否已安装，以及版本是否冲突
        合并所有依赖声明来源（requirements.txt + plugin.yaml）
        返回 {
            'ok': True/False,
            'missing': [缺失的包列表],
            'installed': [已安装的包列表],
            'conflicts': [{name, required, installed}],  # 版本冲突列表
            'has_conflict': True/False,
        }
        """
        deps = self._get_merged_dependencies(plugin_name)
        if not deps:
            return {'ok': True, 'missing': [], 'installed': [], 'conflicts': [], 'has_conflict': False}

        missing = []
        installed = []
        conflicts = []

        for dep in deps:
            pkg_name, operator, required_ver = _parse_version_spec(dep)
            import_name = pkg_name.replace('-', '_').replace('.', '_')

            # 检查包是否已安装
            installed_ver = None
            try:
                installed_ver = importlib.metadata.version(pkg_name)
            except importlib.metadata.PackageNotFoundError:
                try:
                    installed_ver = importlib.metadata.version(import_name)
                except importlib.metadata.PackageNotFoundError:
                    missing.append(dep)
                    continue

            # 包已安装，但版本不满足要求 → 冲突
            if operator and required_ver:
                if not _check_version_compatible(installed_ver, operator, required_ver):
                    conflicts.append({
                        'name': pkg_name,
                        'required': f'{operator}{required_ver}',
                        'installed': installed_ver,
                    })
                    continue

            installed.append(dep)

        has_conflict = len(conflicts) > 0

        return {
            'ok': len(missing) == 0 and not has_conflict,
            'missing': missing,
            'installed': installed,
            'conflicts': conflicts,
            'has_conflict': has_conflict,
        }

    def auto_install_dependencies(self, plugin_name: str) -> dict:
        """
        自动安装插件缺失的 Python 依赖
        只安装 missing 的包，版本冲突的包不自动覆盖
        返回 {'success': True/False, 'installed': [...], 'failed': [...], 'conflicts': [...]}
        """
        result = self.check_dependencies(plugin_name)
        if result['ok']:
            return {'success': True, 'installed': [], 'failed': [], 'conflicts': []}

        installed = []
        failed = []
        # 强制使用当前解释器，避免多 Python 环境安装到错误位置
        pip_exec = sys.executable
        for dep in result['missing']:
            try:
                logger.info(f"[{plugin_name}] 正在安装依赖: {dep}")
                r = pip_install_with_mirror(pip_exec, dep, timeout=120)
                if r['success']:
                    installed.append(dep)
                    logger.info(f"[{plugin_name}] 依赖安装成功: {dep}（镜像: {r['mirror']}）")
                else:
                    failed.append(dep)
                    logger.warning(f"[{plugin_name}] 依赖安装失败: {dep} - {r['error']}")
            except Exception as e:
                failed.append(dep)
                logger.warning(f"[{plugin_name}] 依赖安装失败: {dep} - {e}")

        return {
            'success': len(failed) == 0,
            'installed': installed,
            'failed': failed,
            'conflicts': result['conflicts'],
        }

    def _record_dep_status(self, plugin_name: str, missing: list, conflicts: list):
        """记录插件的依赖状态（缺失 + 冲突，供 Web UI 展示）"""
        with self._lock:
            if missing:
                self._missing_deps[plugin_name] = missing
            else:
                self._missing_deps.pop(plugin_name, None)
            if conflicts:
                self._conflict_deps[plugin_name] = conflicts
            else:
                self._conflict_deps.pop(plugin_name, None)

    def get_dep_status(self, plugin_name: str = None) -> dict:
        """获取依赖状态信息"""
        with self._lock:
            if plugin_name:
                return {
                    'missing': self._missing_deps.get(plugin_name, []),
                    'has_missing': plugin_name in self._missing_deps,
                    'conflicts': self._conflict_deps.get(plugin_name, []),
                    'has_conflict': plugin_name in self._conflict_deps,
                }
            return {
                'missing': dict(self._missing_deps),
                'conflicts': dict(self._conflict_deps),
            }

    def get_missing_deps(self, plugin_name: str) -> dict:
        """获取插件缺失依赖（Web UI 等调用）"""
        return self.get_dep_status(plugin_name)

    def install_missing_deps(self, plugin_name: str) -> dict:
        """
        一键安装插件缺失的依赖（基于全局环境），安装成功后清除缺失记录。
        版本冲突的依赖自动跳过（不覆盖全局包），冲突记录保留展示。
        """
        result = self.auto_install_dependencies(plugin_name)
        if result['success']:
            self._record_dep_status(plugin_name, [], result.get('conflicts', []))
        return result
