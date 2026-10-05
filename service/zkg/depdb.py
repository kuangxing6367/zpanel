"""依赖状态库（类 dpkg 状态库）—— 独立的 plugins.db。

设计（与用户确认）：
- 与运行时/配置 DB 物理分离的独立 SQLite 文件（默认 data/plugins.db）。
- **每次启动重建**（派生缓存，非真相源）。
- 每张官方工具表 ``pkg_<T>`` 按行记录「谁依赖它」（列 ``plugin``）；
  **零行 = 无依赖 → 框架不加载该工具**。
- ``plugins`` 表记录扫描到的用户插件（id / path / enabled），
  供运行时删插件时判定依赖变化。
- 运行时删插件 → 重刷依赖库 → 某工具依赖方变空 → 不再加载（剪枝）。

兼容 SQL 标识符：工具 id 只允许 [A-Za-z0-9_]，非法字符拒绝。
"""

from __future__ import annotations

import os
import re
import sqlite3
from typing import List

from .manifest import Manifest
from .resolver import Resolution

_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _safe_table(tool_id: str) -> str:
    if not _ID_RE.match(tool_id):
        raise ValueError(f"非法工具 id（仅允许字母数字下划线）: {tool_id}")
    return "pkg_" + tool_id


class DepDB:
    def __init__(self, path: str):
        self.path = path

    def rebuild(self, plugin_manifests: List[Manifest], res: Resolution) -> dict:
        """原地重建 plugins.db，返回统计信息。

        语义仍是「每次启动重建」（派生缓存，非真相源），但**不删除文件**：
        先清空既有表再重建。删文件会撞 Windows 文件锁，也会触发外部的
        删除保护导致启动中断；原地重建语义等价且更安全。
        """
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)

        conn = sqlite3.connect(self.path)
        cur = conn.cursor()
        # 清空派生表（保留数据库文件本身）
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND (name='plugins' OR name LIKE 'pkg_%')"
        )
        for (tname,) in cur.fetchall():
            cur.execute(f"DROP TABLE IF EXISTS {tname}")
        cur.execute(
            "CREATE TABLE plugins (id TEXT PRIMARY KEY, path TEXT, enabled INTEGER)"
        )

        for pm in plugin_manifests:
            cur.execute(
                "INSERT OR REPLACE INTO plugins (id, path, enabled) VALUES (?, ?, 1)",
                (pm.id, pm.path or ""),
            )

        stats = {"tools_total": 0, "tools_loaded": 0, "tools_skipped": 0}
        # 为每个官方工具建一张表，行 = 依赖方（needed 含全部工具及其 dependents）
        for tid, v in sorted(res.needed.items()):
            stats["tools_total"] += 1
            tname = _safe_table(tid)
            cur.execute(f"CREATE TABLE {tname} (plugin TEXT)")
            for dep in v["dependents"]:
                cur.execute(f"INSERT INTO {tname} (plugin) VALUES (?)", (dep,))
            rowcount = len(v["dependents"])
            if rowcount == 0:
                stats["tools_skipped"] += 1
            else:
                stats["tools_loaded"] += 1

        conn.commit()
        conn.close()
        return stats

    def list_tool_tables(self) -> List[str]:
        conn = sqlite3.connect(self.path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'pkg_%'")
        names = [r[0] for r in cur.fetchall()]
        conn.close()
        return names

    def tool_dependents(self, tool_id: str) -> List[str]:
        tname = _safe_table(tool_id)
        conn = sqlite3.connect(self.path)
        cur = conn.cursor()
        cur.execute(f"SELECT plugin FROM {tname}")
        rows = [r[0] for r in cur.fetchall()]
        conn.close()
        return rows

    def is_loaded(self, tool_id: str) -> bool:
        """该工具是否「有依赖方」→ 是否加载。零行即不加载。"""
        return len(self.tool_dependents(tool_id)) > 0
