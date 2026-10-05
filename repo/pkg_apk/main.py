# -*- coding: utf-8 -*-
"""pkg_apk — Alpine 家族（apk）。容器镜像里最常见的轻量发行版。"""
import shutil

import zkg

__version__ = "1.0.0"


def _procs():
    mod = zkg.tool("procs")
    if mod is None:
        raise RuntimeError("pkg_apk 依赖 procs 机制包")
    return mod


def applies() -> bool:
    return bool(shutil.which("apk"))


def backend() -> dict:
    exe = shutil.which("apk") or ""
    return {"backend": "apk", "exe": exe, "available": bool(exe)}


def family() -> str:
    return "apk"


def installed(name: str) -> dict:
    r = _procs().run(["apk", "info", "-e", name], timeout=15, shell=False)
    if r.returncode == 0:
        v = _procs().run(["apk", "info", "-e", "-v", name], timeout=15, shell=False)
        return {"installed": True, "version": (v.stdout or "").strip()}
    return {"installed": False, "version": ""}


def owns(path: str) -> dict:
    r = _procs().run(["apk", "info", "--who-owns", path], timeout=15, shell=False)
    out = (r.stdout or "").strip()
    # 输出形如 "path is owned by nginx-1.24.0-r6"
    if r.returncode == 0 and " is owned by " in out:
        return {"pkg": out.rsplit(" is owned by ", 1)[1].strip()}
    return {"pkg": ""}


def candidate(name: str) -> dict:
    r = _procs().run(["apk", "search", "-e", name], timeout=30, shell=False)
    out = (r.stdout or "").strip()
    if r.returncode == 0 and out:
        first = out.splitlines()[0].strip()
        return {"exists": True, "version": first.split("-", 0)[-1] if "-" in first else first}
    return {"exists": False, "version": ""}


def refresh(timeout: float = 300.0) -> dict:
    r = _procs().run(["apk", "update"], timeout=timeout, shell=False)
    log = ((r.stdout or "") + (r.stderr or "")).strip()
    return {"ok": bool(r.ok), "log": log[-1500:]}


def install(names, timeout: float = 900.0) -> dict:
    r = _procs().run(["apk", "add", *names], timeout=timeout, shell=False)
    log = ((r.stdout or "") + (r.stderr or "")).strip()
    return {"ok": bool(r.ok), "returncode": r.returncode, "log": log[-3000:],
            "lock_busy": False, "error": "" if r.ok else log[-400:]}


def lock_status() -> dict:
    return {"busy": False, "holder": ""}


def remove(names, timeout: float = 900.0) -> dict:
    r = _procs().run(["apk", "del", *names], timeout=timeout, shell=False)
    log = ((r.stdout or "") + (r.stderr or "")).strip()
    return {"ok": bool(r.ok), "returncode": r.returncode, "log": log[-3000:],
            "error": "" if r.ok else log[-400:]}
