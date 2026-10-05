# -*- coding: utf-8 -*-
"""文件操作（core/ctx 节点 14）

插件文件读写统一落到自身数据目录（get_data_dir），避免插件越权访问
代码目录或系统任意路径。相对路径基于插件数据目录解析；传入绝对路径
将被拒绝（除非显式 allow_abs=True，仅限高级用法）。
"""
import os


class FileMixin:
    """ctx 上的文件操作（读写均限定在插件数据目录内）。"""

    def read_file(self, name: str, encoding: str = 'utf-8', allow_abs: bool = False) -> str:
        """读取插件数据目录下的文件，返回文本。"""
        path = self._resolve_path(name, allow_abs)
        with open(path, 'r', encoding=encoding) as f:
            return f.read()

    def write_file(self, name: str, content: str, encoding: str = 'utf-8', allow_abs: bool = False):
        """写入插件数据目录下的文件（自动建父目录）。"""
        path = self._resolve_path(name, allow_abs)
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        with open(path, 'w', encoding=encoding) as f:
            f.write(content)

    def list_dir(self, subdir: str = '', include_dirs: bool = False) -> list:
        """列出插件数据目录（或子目录）下的条目，返回相对名列表。"""
        base = self.get_data_dir()
        target = os.path.join(base, subdir) if subdir else base
        if not os.path.isdir(target):
            return []
        out = []
        for entry in os.listdir(target):
            full = os.path.join(target, entry)
            if os.path.isdir(full):
                if include_dirs:
                    out.append(entry + '/')
            else:
                out.append(entry)
        return sorted(out)

    def _resolve_path(self, name: str, allow_abs: bool) -> str:
        if os.path.isabs(name):
            if not allow_abs:
                raise ValueError(
                    f"read_file/write_file 拒绝绝对路径 {name!r}（安全限制）；"
                    f"如需操作数据目录外文件，请传 allow_abs=True 并自担风险"
                )
            return name
        return os.path.join(self.get_data_dir(), name)
