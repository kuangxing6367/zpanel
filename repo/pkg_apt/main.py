# -*- coding: utf-8 -*-
"""pkg_apt — Debian / Ubuntu / Armbian 家族（apt-get + dpkg）。

兼容性事实（2026 实况）：
- Ubuntu 24.04+ 与 Debian 12+ 默认源配置是 **deb822 格式**（/etc/apt/sources.list.d/*.sources），
  旧式 sources.list 仍然支持；两种并存会出现 duplicate 警告 —— 探测时两种都认。
- apt-get install 会被 unattended-upgrades 长时间持有 dpkg 锁 —— lock_status 必须先查。
- 仓库索引可能从没更新过（新装机）→ candidate 查不到 → 调用方应先 refresh 再重查。
"""
import os
import shutil
import subprocess

import zkg

__version__ = "1.0.0"


def _procs():
    mod = zkg.tool("procs")
    if mod is None:
        raise RuntimeError("pkg_apt 依赖 procs 机制包")
    return mod


def applies() -> bool:
    return bool(shutil.which("apt-get") or shutil.which("dpkg"))


def backend() -> dict:
    exe = shutil.which("apt-get") or ""
    return {"backend": "apt", "exe": exe, "available": bool(exe)}


def family() -> str:
    return "deb"


def _sources_layout() -> str:
    """源配置格式：deb822（24.04+ 默认）/ legacy / mixed —— 排障用，不影响操作。"""
    has_new = any(f.endswith(".sources")
                  for f in os.listdir("/etc/apt/sources.list.d")) \
        if os.path.isdir("/etc/apt/sources.list.d") else False
    has_old = os.path.isfile("/etc/apt/sources.list")
    if has_new and has_old:
        return "mixed"
    return "deb822" if has_new else ("legacy" if has_old else "none")


def installed(name: str) -> dict:
    # 输出形如 "install ok installed 1.24.0-2ubuntu4"
    r = _procs().run(["dpkg-query", "-W", "-f=${Status} ${Version}", name],
                     timeout=15, shell=False)
    out = (r.stdout or "").strip()
    parts = out.split(None, 3)
    if r.returncode == 0 and len(parts) >= 3 and parts[2] == "installed":
        return {"installed": True, "version": parts[3] if len(parts) > 3 else ""}
    return {"installed": False, "version": ""}


def owns(path: str) -> dict:
    r = _procs().run(["dpkg", "-S", path], timeout=15, shell=False)
    out = (r.stdout or "").strip()
    if r.returncode == 0 and ":" in out:
        return {"pkg": out.split(":", 1)[0].strip()}
    return {"pkg": ""}


def candidate(name: str) -> dict:
    """查本地仓库索引（不联网）。索引为空时 Candidate: (none) —— 调用方先 refresh。"""
    r = _procs().run(["apt-cache", "policy", name], timeout=20, shell=False)
    out = (r.stdout or "").strip()
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Candidate:"):
            ver = line.split(":", 1)[1].strip()
            exists = bool(ver) and ver != "(none)"
            return {"exists": exists, "version": ver if exists else ""}
    return {"exists": False, "version": ""}


def refresh(timeout: float = 300.0) -> dict:
    env = dict(os.environ)
    env["DEBIAN_FRONTEND"] = "noninteractive"
    r = _procs().run(["apt-get", "update"], timeout=timeout, env=env, shell=False)
    log = ((r.stdout or "") + (r.stderr or "")).strip()
    return {"ok": bool(r.ok), "log": log[-1500:]}


def install(names, timeout: float = 900.0) -> dict:
    st = lock_status()
    if st.get("busy"):
        return {"ok": False, "lock_busy": True, "returncode": -1, "log": "",
                "error": f"包管理器正被占用（{st.get('holder') or '其他进程'}），稍后再试"}
    env = dict(os.environ)
    env["DEBIAN_FRONTEND"] = "noninteractive"
    r = _procs().run(["apt-get", "install", "-y", *names],
                     timeout=timeout, env=env, shell=False)
    log = ((r.stdout or "") + (r.stderr or "")).strip()
    return {"ok": bool(r.ok), "returncode": r.returncode, "log": log[-3000:],
            "lock_busy": False, "error": "" if r.ok else log[-400:]}


def lock_status() -> dict:
    """dpkg/apt 锁被谁持有（unattended-upgrades 是最常见的元凶）。"""
    locks = ["/var/lib/dpkg/lock-frontend", "/var/lib/dpkg/lock",
             "/var/lib/apt/lists/lock"]
    # fuser 不可用时退化为 pgrep 包管理器进程
    for lk in locks:
        if not os.path.exists(lk):
            continue
        r = _procs().run(["fuser", lk], timeout=10, shell=False)
        if r.returncode == 0 and (r.stdout or "").strip():
            pid = (r.stdout or "").split()[0].strip()
            who = _procs().run(["ps", "-o", "cmd=", "-p", pid],
                               timeout=10, shell=False)
            return {"busy": True, "holder": (who.stdout or "").strip() or f"pid {pid}"}
    return {"busy": False, "holder": ""}


def remove(names, timeout: float = 900.0) -> dict:
    """卸载（不用 --purge：保留 /etc 配置与用户数据）。"""
    st = lock_status()
    if st.get("busy"):
        return {"ok": False, "returncode": -1, "log": "",
                "error": f"包管理器正被占用（{st.get('holder') or '其他进程'}），稍后再试"}
    env = dict(os.environ)
    env["DEBIAN_FRONTEND"] = "noninteractive"
    r = _procs().run(["apt-get", "remove", "-y", *names],
                     timeout=timeout, env=env, shell=False)
    log = ((r.stdout or "") + (r.stderr or "")).strip()
    return {"ok": bool(r.ok), "returncode": r.returncode, "log": log[-3000:],
            "error": "" if r.ok else log[-400:]}
