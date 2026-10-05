"""机制包之间的取用接口 —— loader 会把它注册为顶层模块 ``zkg``。

背景：机制包是被 loader **按文件路径**加载的独立模块（模块名 ``<id>_tool``），
彼此之间没有合法包名可以 import。但机制包之间确实存在依赖
（例：``probe -> procs``、``sandbox -> pathsafe``、``vhost -> sandbox``）。

于是 loader 在加载前把本模块注册为顶层 ``zkg``，包内即可直接：

    import zkg
    procs = zkg.tool('procs')

``tool(name)`` 返回**已加载**的机制包模块；未加载返回 None。这几乎总是意味着
``manifest.toml`` 的 ``dependencies`` 漏了声明，或依赖顺序出了问题 ——
包内应当**明确报错**而不是静默降级，否则依赖图就形同虚设。

单独测试某个机制包时，先调用 ``register()`` 即可让包内的 ``import zkg`` 成立。
"""
from __future__ import annotations

import sys

MODNAME = "zkg"
_SUFFIX = "_tool"


class ToolProxy:
    """机制包取用句柄（只读，无状态）。"""

    __slots__ = ()

    def tool(self, name: str):
        """取已加载的机制包模块；未加载返回 None。"""
        if not name:
            return None
        return sys.modules.get(f"{name}{_SUFFIX}")

    def has(self, name: str) -> bool:
        return self.tool(name) is not None

    def loaded(self) -> list:
        """当前已加载的机制包 id 列表（按字母序）。"""
        return sorted(k[:-len(_SUFFIX)] for k in list(sys.modules)
                      if k.endswith(_SUFFIX) and len(k) > len(_SUFFIX))

    def require(self, name: str):
        """取机制包，未加载则抛 RuntimeError（依赖声明的硬校验）。"""
        mod = self.tool(name)
        if mod is None:
            raise RuntimeError(
                f"机制包 {name!r} 未加载：请确认它的 manifest 被扫描到，"
                f"且某个包/插件在 dependencies 里声明了它")
        return mod

    def __repr__(self) -> str:
        return f"<zkg proxy loaded={self.loaded()}>"


_PROXY = ToolProxy()

# 模块级快捷方式（包内 `import zkg; zkg.tool(...)` 与 `from zkg import tool` 都可用）
tool = _PROXY.tool
has = _PROXY.has
loaded = _PROXY.loaded
require = _PROXY.require


def register(module=None) -> object:
    """把本模块注册为顶层 ``zkg``（幂等；重复调用不覆盖已有注册）。"""
    existing = sys.modules.get(MODNAME)
    if existing is None:
        sys.modules[MODNAME] = module if module is not None else sys.modules[__name__]
    return sys.modules[MODNAME]


__all__ = ["register", "tool", "has", "loaded", "require", "ToolProxy", "MODNAME"]
