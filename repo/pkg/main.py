# -*- coding: utf-8 -*-
"""
pkg — 包管理器门面（机制包，零框架依赖）

**这里没有一行发行版分支。** 家族差异全部拆进实现包：

    pkg_apt     Debian / Ubuntu / Armbian      (apt-get + dpkg)
    pkg_rpm     RHEL / CentOS / Fedora / Anolis / TencentOS / openSUSE
                                                   (dnf5 / dnf / yum / zypper + rpm)
    pkg_pacman  Arch / Manjaro                 (pacman)
    pkg_apk     Alpine                         (apk)
    pkg_win     Windows 10/11 / Server 2012+   (winget / choco，没有就如实报不可用)

门面只做两件事：**挑实现**、**委派调用** —— 与 svcmgr 同一套路。
运维面板的第一兼容性就在这层：包名因发行版而异、仓库索引会过期、
dpkg 会被 unattended-upgrades 锁住、二进制可能不是包管理器装的 ——
这些全部在实现包里如实回答，调用方（services 扩展等）不猜。

统一契约（每个实现包必须实现）：

    applies()                        当前机器是否适用
    backend()                        {backend, exe, available}
    family()                         'deb' | 'rpm' | 'pacman' | 'apk'
    installed(name)                  {installed, version}     本机实查
    owns(path)                       {pkg}                    这个文件是谁装的
    candidate(name)                  {exists, version}        仓库里有没有
    refresh(timeout)                 {ok, log}                刷新仓库索引
    install(names, timeout)          {ok, returncode, log, lock_busy}
    remove(names, timeout)           {ok, returncode, log}    卸载（不删配置，留用户数据）
    lock_status()                    {busy, holder}           包管理器是否被占用
"""
from __future__ import annotations

import platform

import zkg

__version__ = "1.0.0"

# 按优先级登记实现包：先 applies() 命中的那个生效
IMPLS = ("pkg_apt", "pkg_rpm", "pkg_pacman", "pkg_apk", "pkg_win")


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
    raise NoBackend(f"当前平台（{platform.system()}）没有适用的包管理器实现包；"
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
        return {"backend": "none", "family": "", "platform": platform.system().lower(),
                "exe": "", "available": False, "error": str(e)}
    b = dict(m.backend())
    b.setdefault("platform", platform.system().lower())
    try:
        b["family"] = m.family()
    except Exception:
        b["family"] = ""
    return b


def family() -> str:
    """'deb' | 'rpm' | 'pacman' | 'apk'，没有包管理器则 ''。"""
    try:
        return impl().family()
    except NoBackend:
        return ""


def installed(name: str) -> dict:
    return impl().installed(str(name or "").strip())


def owns(path: str) -> dict:
    """这个文件属于哪个包 —— 区分「包管理器装的」和「源码/手动的」。"""
    return impl().owns(str(path or "").strip())


def candidate(name: str) -> dict:
    """仓库索引里有没有这个包（不联网查详情，用本地索引）。"""
    return impl().candidate(str(name or "").strip())


def refresh(timeout: float = 300.0) -> dict:
    """刷新仓库索引（apt-get update / dnf makecache / pacman -Sy / apk update）。"""
    return impl().refresh(timeout)


def install(names, timeout: float = 900.0) -> dict:
    """安装若干包。调用方保证 names 来自白名单目录，本层不做白名单校验。"""
    if isinstance(names, str):
        names = [names]
    names = [str(n).strip() for n in (names or []) if str(n).strip()]
    if not names:
        return {"ok": False, "error": "未提供任何包名"}
    return impl().install(names, timeout)


def remove(names, timeout: float = 900.0) -> dict:
    """卸载若干包。**不用 --purge**：保留 /etc 下的配置，用户数据不陪葬。"""
    if isinstance(names, str):
        names = [names]
    names = [str(n).strip() for n in (names or []) if str(n).strip()]
    if not names:
        return {"ok": False, "error": "未提供任何包名"}
    return impl().remove(names, timeout)


def lock_status() -> dict:
    """包管理器是否被别的进程占用（apt 锁 / 运行中的 dnf / pacman db.lck）。"""
    return impl().lock_status()
