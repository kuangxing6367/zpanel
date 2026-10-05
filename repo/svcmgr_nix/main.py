# -*- coding: utf-8 -*-
"""
svcmgr_nix — Linux / macOS 的服务管理实现（机制包，零框架依赖）

**只管 *nix**，不含任何 Windows 分支 —— 平台差异拆成两个包（`svcmgr_win` / `svcmgr_nix`），
由 `svcmgr` 门面按当前平台挑选。这样每个包读起来都是直的，不用在脑子里跑多平台分支。

管理器优先级：systemd → SysV `service` → macOS `brew services` → launchctl。
"""
from __future__ import annotations

import os
import shutil

import zkg

__version__ = "1.0.0"


def _procs():
    mod = zkg.tool("procs")
    if mod is None:
        raise RuntimeError(
            "svcmgr_nix 依赖 procs 机制包，但未被加载 —— 检查 manifest.toml 的 dependencies")
    return mod


def applies() -> bool:
    return os.name != "nt"


def backend() -> dict:
    systemctl = shutil.which("systemctl")
    if systemctl and os.path.isdir("/run/systemd/system"):
        return {"backend": "systemd", "exe": systemctl, "available": True}
    service = shutil.which("service")
    if service:
        return {"backend": "sysv", "exe": service, "available": True}
    brew = shutil.which("brew")
    if brew:
        return {"backend": "brew", "exe": brew, "available": True}
    lc = shutil.which("launchctl")
    return {"backend": "launchctl" if lc else "none", "exe": lc or "", "available": bool(lc)}


def _argv(action: str, name: str) -> list:
    b = backend()["backend"]
    if b == "systemd":
        return ["systemctl", action, name]
    if b == "sysv":
        return ["service", name, action]
    if b == "brew":
        return ["brew", "services", action, name]
    if b == "launchctl":
        # launchctl 的 kickstart/stop 需要 domain target，这里退化为 list 查询
        return ["launchctl", "list", name]
    raise RuntimeError("当前 *nix 机器没有可用的服务管理器")


def status(name: str, timeout: float = 8.0) -> dict:
    pr = _procs()
    try:
        argv = _argv("status", name)
    except RuntimeError as e:
        return {"ok": False, "found": False, "running": None, "raw": "", "error": str(e)}
    r = pr.run(argv, timeout=timeout, shell=False)
    raw = ((r.stdout or "") + (r.stderr or "")).strip()
    low = raw.lower()
    running = ("active (running)" in low or "is running" in low or "started" in low)
    missing = ("could not be found" in low or "not-found" in low or "no such" in low
               or "unit" in low and "loaded" not in low and r.returncode != 0
               and "active" not in low)
    return {"ok": True, "found": not missing, "running": bool(running),
            "raw": raw[:400], "error": "" if not missing else raw[:200]}


def act(action: str, name: str, timeout: float = 25.0) -> dict:
    if action not in ("start", "stop", "restart", "reload", "enable", "disable"):
        return {"ok": False, "action": action, "service": name, "raw": "",
                "error": f"不支持的动作: {action}"}
    pr = _procs()
    try:
        argv = _argv(action, name)
    except RuntimeError as e:
        return {"ok": False, "action": action, "service": name, "raw": "", "error": str(e)}
    try:
        r = pr.run(argv, timeout=timeout, shell=False)
    except Exception as e:          # 权限不足 / 被安全策略拦截
        return {"ok": False, "action": action, "service": name, "raw": "",
                "error": f"{type(e).__name__}: {e}"}
    raw = ((r.stdout or "") + (r.stderr or "")).strip()
    ok = r.returncode == 0 and not r.timed_out
    return {"ok": ok, "action": action, "service": name, "raw": raw[:400],
            "error": "" if ok else (raw[:300] or f"退出码 {r.returncode}")}
