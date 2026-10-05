# -*- coding: utf-8 -*-
"""pkg_pacman — Arch / Manjaro 家族。

注意：pacman 的哲学是「滚动升级整个系统」，-S 安装前索引必须新，
否则容易踩 partial upgrade。refresh 用 -Sy 只刷索引是 Arch 官方不推荐的
（-Syu 才完整）——本层提供 refresh()（-Sy，快）供安装前调用，
调用方若要彻底稳妥应配合 -u。lock 是 /var/lib/pacman/db.lck 文件。
"""
import os
import shutil

import zkg

__version__ = "1.0.0"


def _procs():
    mod = zkg.tool("procs")
    if mod is None:
        raise RuntimeError("pkg_pacman 依赖 procs 机制包")
    return mod


def applies() -> bool:
    return bool(shutil.which("pacman"))


def backend() -> dict:
    exe = shutil.which("pacman") or ""
    return {"backend": "pacman", "exe": exe, "available": bool(exe)}


def family() -> str:
    return "pacman"


def installed(name: str) -> dict:
    r = _procs().run(["pacman", "-Q", name], timeout=15, shell=False)
    out = (r.stdout or "").strip()
    if r.returncode == 0 and " " in out:
        return {"installed": True, "version": out.split(None, 1)[1]}
    return {"installed": False, "version": ""}


def owns(path: str) -> dict:
    r = _procs().run(["pacman", "-Qo", path], timeout=15, shell=False)
    out = (r.stdout or "").strip()
    if r.returncode == 0 and " is owned by " in out:
        return {"pkg": out.rsplit(" is owned by ", 1)[1].strip()}
    return {"pkg": ""}


def candidate(name: str) -> dict:
    r = _procs().run(["pacman", "-Si", name], timeout=30, shell=False)
    out = (r.stdout or "").strip()
    if r.returncode == 0 and "Version" in out:
        ver = ""
        for line in out.splitlines():
            if line.startswith("Version"):
                ver = line.split(":", 1)[1].strip()
                break
        return {"exists": True, "version": ver}
    return {"exists": False, "version": ""}


def refresh(timeout: float = 300.0) -> dict:
    r = _procs().run(["pacman", "-Sy"], timeout=timeout, shell=False)
    log = ((r.stdout or "") + (r.stderr or "")).strip()
    return {"ok": bool(r.ok), "log": log[-1500:]}


def install(names, timeout: float = 900.0) -> dict:
    st = lock_status()
    if st.get("busy"):
        return {"ok": False, "lock_busy": True, "returncode": -1, "log": "",
                "error": f"pacman 数据库被占用（{st.get('holder')}），稍后再试"}
    r = _procs().run(["pacman", "-S", "--noconfirm", "--needed", *names],
                     timeout=timeout, shell=False)
    log = ((r.stdout or "") + (r.stderr or "")).strip()
    return {"ok": bool(r.ok), "returncode": r.returncode, "log": log[-3000:],
            "lock_busy": False, "error": "" if r.ok else log[-400:]}


def lock_status() -> dict:
    lk = "/var/lib/pacman/db.lck"
    if os.path.exists(lk):
        return {"busy": True, "holder": "pacman db.lck 存在"}
    return {"busy": False, "holder": ""}


def remove(names, timeout: float = 900.0) -> dict:
    r = _procs().run(["pacman", "-R", "--noconfirm", *names],
                     timeout=timeout, shell=False)
    log = ((r.stdout or "") + (r.stderr or "")).strip()
    return {"ok": bool(r.ok), "returncode": r.returncode, "log": log[-3000:],
            "error": "" if r.ok else log[-400:]}
