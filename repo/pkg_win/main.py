# -*- coding: utf-8 -*-
"""pkg_win — Windows 家族（Win10/11、Windows Server 2012→2025）。

兼容性事实（2026 实况）：
- **winget 只存在于 Win10 1809+ 装了「应用安装程序」的消费线系统**；
  Windows Server 2012 / 2012R2 / 2016 **没有 winget**，Server 2019/2022 也需要手动装。
- chocolatey 装了就可用（choco.exe 在 PATH）。
- 所以本后端只做两件诚实的事：探测 winget/choco 是否存在；
  都没有就如实报"不可用"，绝不假装能装。服务探测/启停走 svcmgr_win，与包无关。
"""
import os
import platform
import shutil

import zkg

__version__ = "1.0.0"


def _procs():
    mod = zkg.tool("procs")
    if mod is None:
        raise RuntimeError("pkg_win 依赖 procs 机制包")
    return mod


def applies() -> bool:
    return platform.system() == "Windows"


def backend() -> dict:
    winget = shutil.which("winget") or ""
    choco = shutil.which("choco") or ""
    if winget:
        return {"backend": "winget", "exe": winget, "available": True}
    if choco:
        return {"backend": "choco", "exe": choco, "available": True}
    return {"backend": "none", "exe": "", "available": False}


def family() -> str:
    return "win"


def _os_note() -> str:
    """旧版 Windows 的如实声明：Server 2012/2012R2 (build 9200/9600) 已 EOL 且无 winget。"""
    try:
        build = int(platform.version().rsplit(".", 1)[-1])
        if build < 10240:      # 10240 = Windows 10 1507
            return (f"旧版 Windows（build {build}，Server 2012/2012 R2 及更早）"
                    f"已停止官方支持，且没有 winget —— 一键安装不可用")
    except Exception:
        pass
    return ""


def installed(name: str) -> dict:
    b = backend()
    if b["backend"] == "choco":
        r = _procs().run(["choco", "list", "--local-only", "--exact", name],
                         timeout=60, shell=False)
        out = (r.stdout or "").strip()
        if r.returncode == 0:
            lines = [l for l in out.splitlines() if l and not l.startswith("Chocolatey")]
            if lines:
                parts = lines[0].split()
                return {"installed": True, "version": parts[1] if len(parts) > 1 else ""}
        return {"installed": False, "version": ""}
    if b["backend"] == "winget":
        r = _procs().run(["winget", "list", "--id", name, "--exact",
                          "--disable-interactivity"], timeout=60, shell=False)
        out = (r.stdout or "").strip()
        if r.returncode == 0 and name.lower() in out.lower():
            return {"installed": True, "version": ""}
        return {"installed": False, "version": ""}
    return {"installed": False, "version": "", "error": _os_note() or "没有 winget/choco"}


def owns(path: str) -> dict:
    return {"pkg": ""}          # Windows 没有包所有权概念，如实空


def candidate(name: str) -> dict:
    b = backend()
    if b["backend"] == "winget":
        r = _procs().run(["winget", "search", "--id", name, "--exact",
                          "--disable-interactivity"], timeout=60, shell=False)
        if r.returncode == 0 and name.lower() in (r.stdout or "").lower():
            return {"exists": True, "version": ""}
        return {"exists": False, "version": ""}
    if b["backend"] == "choco":
        r = _procs().run(["choco", "search", "--exact", "--limit-output", name],
                         timeout=60, shell=False)
        out = (r.stdout or "").strip()
        if r.returncode == 0 and out:
            first = out.splitlines()[0]
            parts = first.split("|")
            return {"exists": True, "version": parts[1] if len(parts) > 1 else ""}
        return {"exists": False, "version": ""}
    return {"exists": False, "version": "", "error": _os_note() or "没有 winget/choco"}


def refresh(timeout: float = 300.0) -> dict:
    return {"ok": False, "log": "Windows 无需刷新索引"}


def install(names, timeout: float = 900.0) -> dict:
    b = backend()
    if b["backend"] == "winget":
        argv = ["winget", "install", "--id", names[0], "--exact", "--silent",
                "--accept-package-agreements", "--accept-source-agreements",
                "--disable-interactivity"]
    elif b["backend"] == "choco":
        argv = ["choco", "install", "-y", *names]
    else:
        return {"ok": False, "returncode": -1, "log": "", "lock_busy": False,
                "error": _os_note() or "没有 winget/choco，一键安装不可用"}
    r = _procs().run(argv, timeout=timeout, shell=False)
    log = ((r.stdout or "") + (r.stderr or "")).strip()
    return {"ok": bool(r.ok), "returncode": r.returncode, "log": log[-3000:],
            "lock_busy": False, "error": "" if r.ok else log[-400:]}


def lock_status() -> dict:
    return {"busy": False, "holder": ""}


def remove(names, timeout: float = 900.0) -> dict:
    b = backend()
    if b["backend"] == "winget":
        argv = ["winget", "uninstall", "--id", names[0], "--exact", "--silent",
                "--disable-interactivity"]
    elif b["backend"] == "choco":
        argv = ["choco", "uninstall", "-y", *names]
    else:
        return {"ok": False, "returncode": -1, "log": "",
                "error": "没有 winget/choco，无法卸载"}
    r = _procs().run(argv, timeout=timeout, shell=False)
    log = ((r.stdout or "") + (r.stderr or "")).strip()
    return {"ok": bool(r.ok), "returncode": r.returncode, "log": log[-3000:],
            "error": "" if r.ok else log[-400:]}
