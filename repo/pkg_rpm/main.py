# -*- coding: utf-8 -*-
"""pkg_rpm — RPM 家族：RHEL/CentOS/Rocky/Alma/Fedora/Anolis(龙蜥)/TencentOS/openSUSE。

兼容性事实（2026 实况）：
- 包管理器三足：dnf（EL8+/Fedora 41+ 为 dnf5，命令入口仍叫 dnf）、yum（EL7 与
  Anolis/TencentOS 的旧线）、zypper（openSUSE）；底层查询统一用 rpm。
- **redis 已在 Fedora 41+/EL10 被 valkey 取代**（许可证变更），EL9 仍是 redis ——
  包名候选由调用方目录决定，本层只如实回答 candidate()。
- EL 系默认仓库没有 php-fpm 的最新版、没有 mysql（只有 mariadb/MySQL module）——
  candidate() 查不到时由调用方提示"可能需要 EPEL/Remi 源"，不在本层猜。
"""
import shutil

import zkg

__version__ = "1.0.0"


def _procs():
    mod = zkg.tool("procs")
    if mod is None:
        raise RuntimeError("pkg_rpm 依赖 procs 机制包")
    return mod


def _mgr() -> str:
    for m in ("dnf", "yum", "zypper"):
        if shutil.which(m):
            return m
    return ""


def applies() -> bool:
    return bool(_mgr() or shutil.which("rpm"))


def backend() -> dict:
    mgr = _mgr()
    return {"backend": mgr or "rpm", "exe": shutil.which(mgr) or "",
            "available": bool(mgr)}


def family() -> str:
    return "rpm"


def installed(name: str) -> dict:
    r = _procs().run(["rpm", "-q", name], timeout=15, shell=False)
    out = (r.stdout or "").strip()
    if r.returncode == 0 and out and not out.lower().startswith("package"):
        # 形如 nginx-1.24.0-1.el9.aarch64 → 去掉 name 前缀与 .arch 后缀
        ver = out[len(name):].lstrip("-")
        if "." in ver:
            ver = ver.rsplit(".", 1)[0]
        return {"installed": True, "version": ver}
    return {"installed": False, "version": ""}


def owns(path: str) -> dict:
    r = _procs().run(["rpm", "-qf", path], timeout=15, shell=False)
    out = (r.stdout or "").strip()
    if r.returncode == 0 and out and "not owned" not in out.lower():
        return {"pkg": out}
    return {"pkg": ""}


def candidate(name: str) -> dict:
    mgr = _mgr()
    if not mgr:
        return {"exists": False, "version": ""}
    if mgr == "zypper":
        r = _procs().run(["zypper", "--non-interactive", "info", "-t", "package", name],
                         timeout=60, shell=False)
        out = (r.stdout or "").strip()
        if "Information for package" in out:
            ver = ""
            for line in out.splitlines():
                if line.strip().startswith("Version"):
                    ver = line.split(":", 1)[1].strip()
                    break
            return {"exists": True, "version": ver}
        return {"exists": False, "version": ""}
    # dnf / yum：本地索引查询；-q 安静，输出含 Available Packages 段即存在
    r = _procs().run([mgr, "-q", "list", name], timeout=90, shell=False)
    out = (r.stdout or "").strip()
    exists = bool(out) and ("Available Packages" in out or "Installed Packages" in out)
    ver = ""
    if exists:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].split(".")[0] == name:
                ver = parts[1]
    return {"exists": exists, "version": ver}


def refresh(timeout: float = 300.0) -> dict:
    mgr = _mgr()
    if mgr == "zypper":
        argv = ["zypper", "--non-interactive", "refresh"]
    elif mgr == "dnf":
        argv = ["dnf", "-q", "makecache"]
    elif mgr == "yum":
        argv = ["yum", "-q", "makecache", "fast"]
    else:
        return {"ok": False, "log": "没有可用的包管理器"}
    r = _procs().run(argv, timeout=timeout, shell=False)
    log = ((r.stdout or "") + (r.stderr or "")).strip()
    return {"ok": bool(r.ok), "log": log[-1500:]}


def install(names, timeout: float = 900.0) -> dict:
    mgr = _mgr()
    if not mgr:
        return {"ok": False, "returncode": -1, "log": "",
                "error": "没有可用的包管理器", "lock_busy": False}
    if mgr == "zypper":
        argv = ["zypper", "--non-interactive", "install", *names]
    else:
        argv = [mgr, "install", "-y", *names]
    r = _procs().run(argv, timeout=timeout, shell=False)
    log = ((r.stdout or "") + (r.stderr or "")).strip()
    return {"ok": bool(r.ok), "returncode": r.returncode, "log": log[-3000:],
            "lock_busy": False, "error": "" if r.ok else log[-400:]}


def lock_status() -> dict:
    # rpm 世界没有全局锁文件；以「是否有包管理器进程在跑」为准
    for exe in ("dnf", "yum", "zypper"):
        r = _procs().run(["pgrep", "-x", exe], timeout=10, shell=False)
        if r.returncode == 0 and (r.stdout or "").strip():
            pid = (r.stdout or "").split()[0]
            who = _procs().run(["ps", "-o", "cmd=", "-p", pid], timeout=10, shell=False)
            return {"busy": True, "holder": (who.stdout or "").strip() or f"pid {pid}"}
    return {"busy": False, "holder": ""}


def remove(names, timeout: float = 900.0) -> dict:
    mgr = _mgr()
    if not mgr:
        return {"ok": False, "returncode": -1, "log": "",
                "error": "没有可用的包管理器"}
    if mgr == "zypper":
        argv = ["zypper", "--non-interactive", "remove", *names]
    else:
        argv = [mgr, "remove", "-y", *names]
    r = _procs().run(argv, timeout=timeout, shell=False)
    log = ((r.stdout or "") + (r.stderr or "")).strip()
    return {"ok": bool(r.ok), "returncode": r.returncode, "log": log[-3000:],
            "error": "" if r.ok else log[-400:]}
