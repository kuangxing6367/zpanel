"""
插件配置读取（core/ctx 节点 5）

插件配置存于 plugin_configs 表，由 Web UI 通过 _conf_schema.json 定义。
读取带 TTL 缓存，避免 async handler 同步查库阻塞事件循环。
"""

import json
import logging
import time

logger = logging.getLogger('zernus')


class ConfigMixin:
    """ctx.get_config / get_all_config —— 带 TTL 缓存的插件配置读取"""

    def get_config(self, key: str, default=None):
        """
        读取插件配置项（带 TTL 缓存，避免 async handler 同步查库阻塞事件循环）
        :param key: 配置键名
        :param default: 默认值（配置不存在时返回）
        :return: 配置值
        """
        cache_key = (self._plugin_name, key)
        now = time.time()
        # 命中缓存直接返回
        cached = self._config_cache.get(cache_key)
        if cached is not None:
            value, ts = cached
            if now - ts < self._config_cache_ttl:
                return value
            self._config_cache.pop(cache_key, None)
        # 缓存未命中，查库
        try:
            row = self._db.query_one(
                "SELECT config_value FROM plugin_configs WHERE plugin_name = %s AND config_key = %s",
                (self._plugin_name, key)
            )
            if row:
                value = row['config_value']
                # 数据库值为 NULL 时返回 default 并缓存 None 标记
                if value is None:
                    self._config_cache[cache_key] = (default, now)
                    return default
                # 尝试 JSON 解码（非字符串类型）
                try:
                    decoded = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    decoded = value
                self._config_cache[cache_key] = (decoded, now)
                return decoded
            self._config_cache[cache_key] = (default, now)
            return default
        except Exception as e:
            logger.error(f"[{self._plugin_name}] 读取配置 {key} 失败: {e}")
            return default

    def get_all_config(self) -> dict:
        """读取插件所有配置项，返回 {key: value} 字典"""
        try:
            rows = self._db.query(
                "SELECT config_key, config_value FROM plugin_configs WHERE plugin_name = %s",
                (self._plugin_name,)
            )
            result = {}
            for r in rows:
                try:
                    result[r['config_key']] = json.loads(r['config_value'])
                except (json.JSONDecodeError, TypeError):
                    result[r['config_key']] = r['config_value']
            return result
        except Exception as e:
            logger.error(f"[{self._plugin_name}] 读取全部配置失败: {e}")
            return {}
