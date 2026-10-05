# -*- coding: utf-8 -*-
"""
svcmgr — 系统服务管理门面（机制包，零框架依赖）

**这里没有一行平台分支。** 平台差异全部拆进两个实现包：

    svcmgr_win   Windows：sc.exe
    svcmgr_nix   Linux / macOS：systemd → SysV service → brew → launchctl

门面只做两件事：**挑实现**、**委派调用**。新增平台（比如 FreeBSD 的 `service`）
就是加一个包 + 在 `IMPLS` 里登记，不用动这里的逻辑。

为什么不在一个包里写 if windows / else：
跨平台分支会让"这件事在 Windows 上到底怎么跑的"这个问题必须靠脑内执行才能回答，
而且两边共用一批工具函数时，改一边容易踩另一边。一个平台一个包，读起来是直的。
"""
from __future__ import annotations

import platform

import zkg

__version__ = "1.0.0"

# 按优先级登记实现包：先 applies() 命中的那个生效
IMPLS = ("svcmgr_win", "svcmgr_nix")


class NoBackend(RuntimeError):
    """当前平台没有任何一个实现包适用。"""


def impl():
    """返回适用于当前平台的实现包模块。"""
    for tid in IMPLS:
        mod = zkg.tool(tid)
        if mod is None:
            continue
        try:
            if mod.applies():
                return mod
        except Exception:
            continue
    raise NoBackend(f"当前平台（{platform.system()}）没有适用的服务管理实现包；"
                    f"已登记：{', '.join(IMPLS)}")


def platforms() -> list:
    """各实现包的适用情况（界面/排障用）。"""
    out = []
    for tid in IMPLS:
        mod = zkg.tool(tid)
        if mod is None:
            out.append({"id": tid, "loaded": False, "applies": False, "backend": ""})
            continue
        try:
            b = mod.backend()
            out.append({"id": tid, "loaded": True, "applies": bool(mod.applies()),
                        "backend": b.get("backend", ""),
                        "available": bool(b.get("available")),
                        "exe": b.get("exe", "")})
        except Exception as e:
            out.append({"id": tid, "loaded": True, "applies": False,
                        "backend": "", "error": f"{type(e).__name__}: {e}"})
    return out


def backend() -> dict:
    try:
        m = impl()
    except NoBackend as e:
        return {"backend": "none", "platform": platform.system().lower(),
                "exe": "", "available": False, "error": str(e)}
    b = dict(m.backend())
    b.setdefault("platform", platform.system().lower())
    b["impl"] = m.__name__ if hasattr(m, "__name__") else "?"
    return b


def status(name: str, timeout: float = 8.0) -> dict:
    return impl().status(name, timeout)


def act(action: str, name: str, timeout: float = 25.0) -> dict:
    return impl().act(action, name, timeout)


def resolve(candidates) -> dict:
    """按候选服务名逐个探测，返回第一个真实存在的。

    服务名在各平台/发行版上不统一（mysqld / mysql / MySQL80…），
    所以只能逐个试，一个都没中就如实说"未找到"，不拿第一个去假装。
    """
    m = impl()
    tried = []
    for n in candidates:
        if not n:
            continue
        tried.append(n)
        st = m.status(n)
        if st.get("found"):
            st["name"] = n
            return st
    return {"ok": False, "found": False, "running": None, "name": "",
            "error": f"未找到服务（已试：{', '.join(str(t) for t in tried)}）"}
