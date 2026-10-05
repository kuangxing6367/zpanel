"""
插件发现 / 配置文件读写 / 配置 schema mixin

负责：扫描插件目录、读写 plugins_dat 下的 plugin.yaml / _conf_schema.json /
README 等配置与文档文件、把代码与配置分离、根据 schema 初始化 plugin_configs 表。
"""
import json
import logging
import os
import re
import shutil

import yaml

logger = logging.getLogger('zernus')


# ── 配置文件后缀定义（这些文件存放在 plugins_dat，而非 plugins） ──
# 后缀匹配（.txt 不自动归类，因为可能是数据文件/requirements.txt）
_CONFIG_FILE_EXTS = ('.yaml', '.yml', '.toml', '.cfg', '.ini', '.md')
# 明确的配置文件名（无论后缀，都归类为配置文件）
_CONFIG_FILE_NAMES = {
    'plugin.yaml', '_conf_schema.json', 'metadata.yaml',
    'README.md', 'README_zh.md', 'README_ru.md',
    'CHANGELOG.md', 'LICENSE',
}
# 排除名单：这些文件虽然后缀匹配，但属于代码/构建文件，跟代码走
_CODE_FILE_NAMES = {
    'requirements.txt', 'package.json', 'package-lock.json',
    'pyproject.toml', 'setup.cfg', 'tox.ini',
}


class ConfigMixin:
    """插件发现与配置文件管理。"""

    def discover(self) -> list:
        """扫描插件目录，返回所有插件目录名列表"""
        plugins = []
        if not os.path.isdir(self.plugins_dir):
            os.makedirs(self.plugins_dir, exist_ok=True)
            return plugins

        for name in os.listdir(self.plugins_dir):
            main_path = os.path.join(self.plugins_dir, name, 'main.py')
            if os.path.isfile(main_path):
                plugins.append(name)
        return plugins

    def _plugin_dat_dir(self, plugin_name: str) -> str:
        """获取插件的数据/配置目录路径"""
        return os.path.join(self.plugins_dat_dir, plugin_name)

    def _plugin_code_dir(self, plugin_name: str) -> str:
        """获取插件的代码目录路径"""
        return os.path.join(self.plugins_dir, plugin_name)

    def ensure_plugins_dat_dir(self, plugin_name: str):
        """确保插件的 plugins_dat 子目录存在"""
        dat_dir = self._plugin_dat_dir(plugin_name)
        if not os.path.isdir(dat_dir):
            os.makedirs(dat_dir, exist_ok=True)
        return dat_dir

    @staticmethod
    def _is_config_file(filename: str) -> bool:
        """判断文件是否属于配置/数据文件（应存放在 plugins_dat）"""
        lower = filename.lower()
        # 排除名单优先（代码/构建文件跟代码走）
        if lower in _CODE_FILE_NAMES:
            return False
        # 明确的配置文件名
        if lower in _CONFIG_FILE_NAMES:
            return True
        # 后缀匹配
        return lower.endswith(_CONFIG_FILE_EXTS)

    def split_installed_files(self, plugin_name: str):
        """
        将 plugins/<name>/ 下的配置文件迁移到 plugins_dat/<name>/
        在插件上传/更新后调用，确保代码和配置分离
        """
        code_dir = self._plugin_code_dir(plugin_name)
        dat_dir = self.ensure_plugins_dat_dir(plugin_name)

        if not os.path.isdir(code_dir):
            return

        for name in os.listdir(code_dir):
            fpath = os.path.join(code_dir, name)
            if not os.path.isfile(fpath):
                continue
            if self._is_config_file(name):
                dest = os.path.join(dat_dir, name)
                # plugin.yaml 是插件元信息（版本/更新源/依赖声明），随插件更新：
                # 始终用代码里的新版覆盖 plugins_dat 旧版，避免旧元信息遮挡新配置。
                if name.lower() == 'plugin.yaml':
                    if os.path.exists(dest):
                        try:
                            os.remove(dest)
                            logger.debug(f"[{plugin_name}] 覆盖旧 plugin.yaml")
                        except Exception as e:
                            logger.warning(f"[{plugin_name}] 删除旧 plugin.yaml 失败: {e}")
                    shutil.move(fpath, dest)
                    logger.debug(f"[{plugin_name}] plugin.yaml 已更新")
                    continue
                # 其他配置文件（_conf_schema.json / README 等）：随插件更新始终覆盖
                # 保证 WebUI 能显示新版配置字段
                if os.path.exists(dest):
                    try:
                        os.remove(dest)
                    except Exception as e:
                        logger.warning(f"[{plugin_name}] 删除旧 {name} 失败: {e}")
                shutil.move(fpath, dest)
                logger.debug(f"[{plugin_name}] 配置文件已更新: {name}")

    def migrate_legacy_configs(self):
        """
        迁移旧版插件：将 plugins/ 下所有插件的配置文件迁移到 plugins_dat/
        在框架启动时调用一次，兼容升级
        """
        if not os.path.isdir(self.plugins_dir):
            return
        migrated = 0
        for name in os.listdir(self.plugins_dir):
            plugin_dir = os.path.join(self.plugins_dir, name)
            if not os.path.isdir(plugin_dir):
                continue
            # 检查是否有配置文件需要迁移
            has_config = any(
                os.path.isfile(os.path.join(plugin_dir, f)) and self._is_config_file(f)
                for f in os.listdir(plugin_dir)
            )
            if has_config:
                self.split_installed_files(name)
                migrated += 1
        if migrated > 0:
            logger.info(f"已将 {migrated} 个插件的配置文件迁移到 plugins_dat/")

    def read_plugin_yaml(self, plugin_name: str) -> dict:
        """
        读取插件的 plugin.yaml 配置文件
        读取顺序（优先级从高到低）：
          1. plugins_dat/<name>/plugin.yaml    — 用户数据目录（配置会被迁移到此）
          2. plugins/<name>/plugin.yaml        — 代码目录（首次加载或未迁移时的 fallback）
        返回 dict，如果不存在返回空 dict
        """
        # 1. 优先读取 plugins_dat（用户可编辑版本）
        yaml_path = os.path.join(self.plugins_dat_dir, plugin_name, 'plugin.yaml')
        if os.path.isfile(yaml_path):
            try:
                with open(yaml_path, 'r', encoding='utf-8') as f:
                    return yaml.safe_load(f) or {}
            except Exception as e:
                logger.warning(f"[{plugin_name}] 读取 plugins_dat/plugin.yaml 失败: {e}，回退到代码目录")

        # 2. fallback: 读取代码目录下的 plugin.yaml（首次加载未迁移时）
        yaml_path = os.path.join(self.plugins_dir, plugin_name, 'plugin.yaml')
        if os.path.isfile(yaml_path):
            try:
                with open(yaml_path, 'r', encoding='utf-8') as f:
                    return yaml.safe_load(f) or {}
            except Exception as e:
                logger.warning(f"[{plugin_name}] 读取代码目录 plugin.yaml 失败: {e}")
                return {}

        return {}

    def read_config_schema(self, plugin_name: str) -> dict:
        """
        读取插件的 _conf_schema.json 配置 schema（从 plugins_dat 读取）
        返回 {key: {type, description, default, hint, options, ...}} 格式
        如果不存在返回空 dict

        兜底：插件没有 _conf_schema.json 时，扫描 plugin_configs 表中
        该插件已有的配置项，自动生成基础 schema（type 按值类型推断），
        让 Web 配置页仍能显示并编辑插件运行中写入的配置项。
        """
        schema_path = os.path.join(self.plugins_dat_dir, plugin_name, '_conf_schema.json')
        if os.path.isfile(schema_path):
            try:
                with open(schema_path, 'r', encoding='utf-8') as f:
                    schema = json.loads(f.read())
                if schema:
                    return schema
            except Exception as e:
                logger.warning(f"[{plugin_name}] 读取 _conf_schema.json 失败: {e}")

        # 兜底一：从插件源码静态提取 get_config("key", default) 调用
        try:
            code_dir = os.path.join(self.plugins_dir, plugin_name)
            schema_from_src = {}
            for root, _, fnames in os.walk(code_dir):
                for fn in fnames:
                    if not fn.endswith('.py'):
                        continue
                    src_path = os.path.join(root, fn)
                    try:
                        with open(src_path, 'r', encoding='utf-8', errors='ignore') as f:
                            src = f.read()
                    except Exception:
                        continue
                    # 匹配 get_config("key", default) 及带别名的 _get_config("key", default)
                    for m in re.finditer(
                        r'(?:ctx\.|self\.|plugin\.)?get_config\(\s*(["\'])([^"\']+)\1\s*(?:,\s*(.*?))?\s*\)',
                        src
                    ):
                        key = m.group(2)
                        if not key or key in schema_from_src:
                            continue
                        default = m.group(3)
                        spec = {'type': 'string', 'description': key}
                        if default is not None:
                            d = default.strip()
                            if d == 'True' or d == 'False':
                                spec['type'] = 'bool'
                                spec['default'] = d == 'True'
                            elif d == 'None' or d == '':
                                spec['default'] = None
                            elif d.lstrip('-').isdigit():
                                spec['type'] = 'int'
                                spec['default'] = int(d)
                            elif d.replace('.', '', 1).lstrip('-').isdigit():
                                spec['type'] = 'float'
                                spec['default'] = float(d)
                            elif d.startswith('"') and d.endswith('"'):
                                spec['default'] = d[1:-1]
                            elif d.startswith("'") and d.endswith("'"):
                                spec['default'] = d[1:-1]
                        spec['description'] = f'自动发现（源码中 {fn} 调用 get_config）'
                        schema_from_src[key] = spec
            if schema_from_src:
                return schema_from_src
        except Exception as e:
            logger.warning(f"[{plugin_name}] 源码分析生成 schema 失败: {e}")

        # 兜底二：从 plugin_configs 表已有配置项生成基础 schema
        try:
            rows = self.db.query(
                "SELECT config_key, config_value FROM plugin_configs WHERE plugin_name = %s ORDER BY id",
                (plugin_name,)
            )
            if not rows:
                return {}
            schema = {}
            for r in rows:
                key = r['config_key']
                raw = r['config_value']
                val = None
                try:
                    val = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    val = raw
                if isinstance(val, bool):
                    val_type = 'bool'
                elif isinstance(val, int):
                    val_type = 'int'
                elif isinstance(val, float):
                    val_type = 'float'
                elif isinstance(val, (list, dict)):
                    val_type = 'text'
                else:
                    val_type = 'string'
                schema[key] = {
                    'type': val_type,
                    'description': f'当前值: {val}（由插件运行时配置自动生成）',
                    'default': val if not isinstance(val, (list, dict)) else None,
                }
            return schema
        except Exception as e:
            logger.warning(f"[{plugin_name}] 扫描 plugin_configs 生成 schema 失败: {e}")
            return {}

    def init_plugin_configs(self, plugin_name: str):
        """
        根据 _conf_schema.json 初始化插件配置到 plugin_configs 表
        - 新增字段：插入默认值
        - 不再存在于 schema 的旧字段：删除
        - 已有字段：保持用户已设的值不动
        """
        schema = self.read_config_schema(plugin_name)
        if not schema:
            return

        schema_keys = set(schema.keys())
        try:
            rows = self.db.query(
                "SELECT config_key FROM plugin_configs WHERE plugin_name = %s",
                (plugin_name,)
            )
            existing_keys = {r['config_key'] for r in (rows or [])}
        except Exception as e:
            logger.error(f"[{plugin_name}] 读取现有配置失败: {e}")
            return

        for key in existing_keys - schema_keys:
            try:
                self.db.execute(
                    "DELETE FROM plugin_configs WHERE plugin_name = %s AND config_key = %s",
                    (plugin_name, key)
                )
                logger.debug(f"[{plugin_name}] 已删除废弃配置: {key}")
            except Exception as e:
                logger.warning(f"[{plugin_name}] 删除废弃配置 {key} 失败: {e}")

        for key, spec in schema.items():
            if not isinstance(spec, dict) or key in existing_keys:
                continue
            default = spec.get('default')
            cv = json.dumps(default, ensure_ascii=False) if default is not None else 'null'
            try:
                self.db.execute(
                    "INSERT INTO plugin_configs (plugin_name, config_key, config_value) "
                    "VALUES (%s, %s, %s)",
                    (plugin_name, key, cv)
                )
            except Exception as e:
                logger.error(f"[{plugin_name}] 初始化配置 {key} 失败: {e}")

    def get_plugin_config_files(self, plugin_name: str) -> list:
        """获取插件数据目录（plugins_dat）下所有配置/文档文件列表"""
        dat_dir = self._plugin_dat_dir(plugin_name)
        if not os.path.isdir(dat_dir):
            return []
        result = []
        for name in os.listdir(dat_dir):
            fpath = os.path.join(dat_dir, name)
            if os.path.isfile(fpath) and name.endswith(_CONFIG_FILE_EXTS):
                size = os.path.getsize(fpath)
                result.append({'name': name, 'size': size})
        return result

    def read_plugin_file(self, plugin_name: str, filename: str) -> str:
        """读取插件数据目录（plugins_dat）下的指定文件内容"""
        # 安全检查：防止路径穿越
        if '..' in filename or '/' in filename or '\\' in filename:
            raise ValueError('非法文件名')
        fpath = os.path.join(self.plugins_dat_dir, plugin_name, filename)
        if not os.path.isfile(fpath):
            raise FileNotFoundError(f'文件不存在: {filename}')
        with open(fpath, 'r', encoding='utf-8') as f:
            return f.read()
