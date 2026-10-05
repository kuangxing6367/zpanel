# -*- coding: utf-8 -*-
"""序列化（core/ctx 节点 16）

提供 load_json/dump_json（标准库 json）与 load_yaml/dump_yaml
（pyyaml，若已安装）。避免插件各自 import 时风格不一、缺依赖报错。
"""
import json as _json

try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _yaml = None
    _HAS_YAML = False


class SerializeMixin:
    """ctx 上的 JSON / YAML 序列化便捷方法。"""

    def load_json(self, text: str, default=None):
        """解析 JSON 文本，失败返回 default（默认 None）。"""
        if text is None:
            return default
        try:
            return _json.loads(text)
        except (ValueError, TypeError):
            return default

    def dump_json(self, obj, ensure_ascii: bool = False, indent: int = 2) -> str:
        """序列化对象为 JSON 文本（默认中文可读、带缩进）。"""
        return _json.dumps(obj, ensure_ascii=ensure_ascii, indent=indent, default=str)

    def load_yaml(self, text: str, default=None):
        """解析 YAML 文本（需 pyyaml）。"""
        if not _HAS_YAML:
            raise ImportError("load_yaml 需要 pyyaml：pip install pyyaml")
        if text is None:
            return default
        try:
            return _yaml.safe_load(text)
        except Exception:
            return default

    def dump_yaml(self, obj) -> str:
        """序列化对象为 YAML 文本（需 pyyaml）。"""
        if not _HAS_YAML:
            raise ImportError("dump_yaml 需要 pyyaml：pip install pyyaml")
        return _yaml.safe_dump(obj, allow_unicode=True, sort_keys=False)
