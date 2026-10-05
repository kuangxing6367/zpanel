"""sandbox —— 受控路径沙箱（机制包）。

框架提供的一等公民程序化接口：凡「用户可控路径」都必须先过这里，
再交给 open() / os.makedirs() / shutil 使用；路径安全只在机制层实现一次。

稳定 API 表面：
    Sandbox(roots, *, resolve_links=True)
        .roots                       规范化后的根目录列表
        .contains(path) -> bool
        .resolve(path) -> str        规范化 + 白名单校验；越界抛 OutOfRoot
        .join(root, *parts) -> str   在指定根下拼接并校验
        .pick(path, default=None)    校验通过返回路径，否则返回 default
    OutOfRoot(ValueError)
    normalize(path, *, resolve_links=True) -> str
    is_within(base, path, *, resolve_links=True) -> bool

设计约定：
- ``resolve_links=True``（默认、推荐）：走 ``os.path.realpath``，解析符号链接与
  Windows junction —— **软链指向外部同样被拒**。这是必须的：不做 realpath 的
  「字符串前缀判断」可以被一个软链绕过，是真实世界最常见的越权路径。
- ``resolve_links=False``：纯 ``abspath`` 语义，只防 ``../`` 穿越。
  该语义**直接复用 pathsafe 机制包**（依赖已在 manifest 声明），不在本包重复实现。
- 根目录不存在也允许配置（如待创建的站点目录），判定按规范化后的字面路径进行。
- Windows 按 normcase 比较（大小写不敏感）；跨盘符（commonpath 抛错）判定越界。
"""
from __future__ import annotations

import os

import zkg                                  # 由 zkg loader 注入（见 service/zkg/api.py）


class OutOfRoot(ValueError):
    """路径越界（目标不在允许的根目录内）。"""


def _pathsafe():
    """取 pathsafe 机制包（manifest 已声明依赖，取不到说明加载顺序出了问题）。"""
    mod = zkg.tool("pathsafe")
    if mod is None:
        raise RuntimeError("sandbox 依赖 pathsafe 机制包，但未被加载"
                           "（检查 manifest.dependencies / 加载顺序）")
    return mod


# ── 规范化 ───────────────────────────────────────────────
def normalize(path, *, resolve_links: bool = True) -> str:
    """规范化路径：realpath（默认）或 abspath，再做 normpath。"""
    p = os.fspath(path)
    p = os.path.realpath(p) if resolve_links else os.path.abspath(p)
    return os.path.normpath(p)


def is_within(base, path, *, resolve_links: bool = True) -> bool:
    """判断 path 是否位于 base 内（含 base 自身）。跨盘符返回 False。"""
    if not resolve_links:
        # 纯字符串语义（abspath + commonpath）与 pathsafe 完全一致 —— 复用，不重写
        return _pathsafe().is_within(base, path)
    b = os.path.normcase(os.path.realpath(os.fspath(base)))
    p = os.path.normcase(os.path.realpath(os.fspath(path)))
    try:
        return os.path.commonpath([b, p]) == b
    except ValueError:      # 不同驱动器（Windows）/ 绝对与相对混用
        return False


class Sandbox:
    """一组允许访问的根目录构成的沙箱。"""

    def __init__(self, roots, *, resolve_links: bool = True):
        if isinstance(roots, (str, bytes, os.PathLike)):
            roots = [roots]
        self.resolve_links = bool(resolve_links)
        # 根目录自身一律取 realpath：否则「根是软链」时比较基准就不稳定
        self.roots = [normalize(r, resolve_links=True) for r in roots if r]

    # ── 判定 ──────────────────────────────────────────────
    def contains(self, path) -> bool:
        """path 是否落在任一允许根内（不抛异常）。"""
        return any(is_within(r, path, resolve_links=self.resolve_links)
                   for r in self.roots)

    def resolve(self, path) -> str:
        """规范化并要求落在允许根内；越界抛 OutOfRoot。

        返回**规范化后的绝对路径** —— 调用方必须使用返回值，而不是原始入参，
        否则等于绕过了本次校验（TOCTOU 的经典形态）。
        """
        abs_path = normalize(path, resolve_links=self.resolve_links)
        if not self.contains(abs_path):
            allowed = ", ".join(self.roots) or "(空)"
            raise OutOfRoot(f"路径越界: {abs_path!r} 不在允许范围 [{allowed}] 内")
        return abs_path

    def join(self, root, *parts) -> str:
        """在 root 下拼接 parts 并校验（root 自身也必须在沙箱内）。"""
        base = self.resolve(root)
        return self.resolve(os.path.join(base, *parts))

    def pick(self, path, default=None):
        """校验通过返回规范化路径，越界返回 default（不抛异常）。"""
        try:
            return self.resolve(path)
        except OutOfRoot:
            return default

    def __len__(self) -> int:
        return len(self.roots)

    def __repr__(self) -> str:
        return f"Sandbox(roots={self.roots!r}, resolve_links={self.resolve_links})"
