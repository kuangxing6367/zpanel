# -*- coding: utf-8 -*-
"""
svcmgr_win — Windows 的服务管理实现（机制包，零框架依赖）

**只管 Windows**，不含任何 *nix 分支。通过 `sc.exe`（服务控制管理器命令行）操作，
不调用 PowerShell 也不依赖 WMI —— 只要系统有 `sc` 就能用，Server Core 上也一样。

语义对照（与 *nix 的 start/stop/status 对齐）：

| 动作 | 命令 |
| --- | --- |
| status | `sc query <name>` |
| start  | `sc start <name>` |
| stop   | `sc stop <name>` |
| restart | stop → 等待 → start（sc 没有 restart 子命令） |

`sc start` 对**已启动**的服务会返回错误码 1056，这里把它判定为成功（幂等），
否则"重启一个已经在跑的服务"会被报成失败 —— 那是误报。
"""
from __future__ import annotations

import os
import shutil
import time

import zkg

__version__ = "1.0.0"

# sc 的常见错误码（十进制）
_ERR_NOT_EXIST = "1060"      # The specified service does not exist
_ERR_ALREADY_RUNNING = "1056"  # service is already running
_ERR_NOT_STARTED = "1062"      # service has not been started


def _procs():
    mod = zkg.tool("procs")
    if mod is None:
        raise RuntimeError(
            "svcmgr_win 依赖 procs 机制包，但未被加载 —— 检查 manifest.toml 的 dependencies")
    return mod


def applies() -> bool:
    return os.name == "nt"


def backend() -> dict:
    exe = shutil.which("sc") or ""
    return {"backend": "sc", "exe": exe, "available": bool(exe)}


def status(name: str, timeout: float = 8.0) -> dict:
    pr = _procs()
    if not backend()["available"]:
        return {"ok": False, "found": False, "running": None, "raw": "",
                "error": "未找到 sc.exe"}
    r = pr.run(["sc", "query", name], timeout=timeout, shell=False)
    raw = ((r.stdout or "") + (r.stderr or "")).strip()
    low = raw.lower()
    if _ERR_NOT_EXIST in raw:
        return {"ok": True, "found": False, "running": None, "raw": raw[:400],
                "error": "服务不存在"}
    running = "running" in low and "stopped" not in low
    return {"ok": True, "found": True, "running": bool(running),
            "raw": raw[:400], "error": ""}


def act(action: str, name: str, timeout: float = 25.0) -> dict:
    if action in ("enable", "disable"):
        return {"ok": False, "action": action, "service": name, "raw": "",
                "error": "Windows 的开/关机自启用 sc config 设置，本包不代劳（需改注册表语义）"}
    if action not in ("start", "stop", "restart", "reload"):
        return {"ok": False, "action": action, "service": name, "raw": "",
                "error": f"不支持的动作: {action}"}
    pr = _procs()
    if not backend()["available"]:
        return {"ok": False, "action": action, "service": name, "raw": "",
                "error": "未找到 sc.exe"}

    if action == "restart":
        s1 = act("stop", name, timeout)
        # 等它真的停下来，最多 8 秒；sc stop 是异步的
        for _ in range(16):
            if not status(name)["running"]:
                break
            time.sleep(0.5)
        s2 = act("start", name, timeout)
        return {"ok": s2["ok"], "action": "restart", "service": name,
                "raw": (s1["raw"] + "\n" + s2["raw"])[:400],
                "error": s2["error"] or ("" if s1["ok"] else s1["error"])}

    if action == "reload":
        action = "stop"      # Windows 服务没有通用 reload，退化处理并如实说明
    try:
        r = pr.run(["sc", action, name], timeout=timeout, shell=False)
    except Exception as e:   # 权限不足 / 被安全策略拦截
        return {"ok": False, "action": action, "service": name, "raw": "",
                "error": f"{type(e).__name__}: {e}"}
    raw = ((r.stdout or "") + (r.stderr or "")).strip()
    already = _ERR_ALREADY_RUNNING in raw or _ERR_NOT_STARTED in raw
    ok = (r.returncode == 0) or already      # 已在目标状态视为成功（幂等）
    return {"ok": ok, "action": action, "service": name, "raw": raw[:400],
            "error": "" if ok else (raw[:300] or f"退出码 {r.returncode}")}
