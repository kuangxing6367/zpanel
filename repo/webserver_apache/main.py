# -*- coding: utf-8 -*-
"""webserver_apache — Apache httpd 适配器（webserver 契约的实现）。

布局事实（2026 实况）：
- Debian/Ubuntu：/etc/apache2/{sites-available,sites-enabled}，启用 = 软链；
- RHEL/CentOS/Rocky/Alma/Anolis/TencentOS：/etc/httpd/conf.d/*.conf 直落；
- 控制命令：apache2ctl（Debian 系）/ apachectl（RHEL 系），-t 校验、-k graceful 重载。

能力如实声明：static / php（mod_proxy_fcgi 反代到 php-fpm）/ proxy 支持；
**ssl 暂未实现**（capabilities 里就是 False，调用方据此提示，不装假能耐）。
"""
import os
import shutil

import zkg

__version__ = "1.0.0"

adapter_id = "apache"
name = "Apache httpd"

MARK = "# managed by zpanel"


def _probe():
    mod = zkg.tool("probe")
    if mod is None:
        raise RuntimeError("webserver_apache 依赖 probe 机制包")
    return mod


def _ctl() -> str:
    return shutil.which("apache2ctl") or shutil.which("apachectl") or ""


def _bin() -> str:
    return _probe().which("apache2", extra_paths=("/usr/sbin",)) or \
        _probe().which("httpd", extra_paths=("/usr/sbin",)) or ""


def _layout() -> dict:
    """发行版布局探测，如实报告 —— 不猜。"""
    if os.path.isdir("/etc/apache2"):
        return {"style": "debian",
                "avail": "/etc/apache2/sites-available",
                "enabled": "/etc/apache2/sites-enabled"}
    if os.path.isdir("/etc/httpd"):
        return {"style": "rhel", "confd": "/etc/httpd/conf.d"}
    return {"style": "unknown"}


def applies() -> bool:
    return bool(_bin() or _ctl())


def detect() -> dict:
    exe = _bin()
    info = _probe().probe("apache", exes=["apache2", "httpd"], args=("-v",),
                          extra_paths=("/usr/sbin",)) if exe else {"version": ""}
    return {
        "id": adapter_id, "name": name,
        "available": bool(exe and _ctl()),
        "exe": exe, "version": str(info.get("version") or ""),
        "layout": _layout(),
        "capabilities": {"static": True, "php": True, "proxy": True, "ssl": False},
    }


# ── 渲染 ────────────────────────────────────────────────
def _vhost(s: dict) -> str:
    doms = [str(d) for d in (s.get("domains") or []) if str(d).strip()]
    server_name = doms[0] if doms else "_default_"
    aliases = " ".join(doms[1:])
    root = str(s.get("root_dir") or "")
    index = str(s.get("index") or "index.html")
    kind = str(s.get("kind") or "static")
    log_dir = str(s.get("log_dir") or "")
    sid = str(s.get("id") or "site")

    lines = [
        MARK,
        f"# site: {s.get('name')} ({sid})",
        "<VirtualHost *:80>",
        f"    ServerName {server_name}",
    ]
    if aliases:
        lines.append(f"    ServerAlias {aliases}")
    if kind == "proxy":
        target = str(s.get("target") or "").rstrip("/") or "http://127.0.0.1:8080"
        lines += [
            "    ProxyPreserveHost On",
            f"    ProxyPass / {target}/",
            f"    ProxyPassReverse / {target}/",
        ]
    else:
        lines += [
            f"    DocumentRoot \"{root}\"",
        ]
        if index:
            lines.append(f"    DirectoryIndex {index}")
        if root:
            lines += [
                f"    <Directory \"{root}\">",
                "        Options -Indexes +FollowSymLinks",
                "        AllowOverride All",
                "        Require all granted",
                "    </Directory>",
            ]
        if kind == "php":
            sock = str(s.get("php_socket") or "127.0.0.1:9000")
            handler = (f"proxy:unix://{sock[5:]}|fcgi://localhost"
                       if sock.startswith("unix:") else f"proxy:fcgi://{sock}")
            lines += [
                "    <FilesMatch \\.php$>",
                f"        SetHandler \"{handler}\"",
                "    </FilesMatch>",
            ]
    if log_dir:
        lines += [
            f"    ErrorLog \"{os.path.join(log_dir, sid + '.error.log')}\"",
            f"    CustomLog \"{os.path.join(log_dir, sid + '.access.log')}\" combined",
        ]
    lines.append("</VirtualHost>")
    return "\n".join(lines) + "\n"


def render(sites: list) -> dict:
    return {f"zpanel-{s.get('id')}.conf": _vhost(s) for s in sites}


# ── 落盘 + 清理 ─────────────────────────────────────────
def deploy(sites: list, conf_dir: str = "") -> dict:
    lay = _layout()
    if lay["style"] == "unknown":
        return {"written": [], "removed": [], "kept": [], "total": 0,
                "error": "未找到 Apache 配置布局（/etc/apache2 或 /etc/httpd）"}

    wanted = render(sites)
    written, removed, kept = [], [], []

    if lay["style"] == "rhel":
        target_dir = lay["confd"]
        os.makedirs(target_dir, exist_ok=True)
        for fname, content in sorted(wanted.items()):
            path = os.path.join(target_dir, fname)
            old = _read(path)
            if old != content:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                written.append(fname)
            else:
                kept.append(fname)
        for fname in sorted(os.listdir(target_dir)):
            if fname in wanted or not fname.startswith("zpanel-") or not fname.endswith(".conf"):
                continue                      # 只碰自家托管文件
            path = os.path.join(target_dir, fname)
            if _read(path, first_line=True) == MARK:
                os.remove(path)
                removed.append(fname)
        return {"written": written, "removed": removed, "kept": kept,
                "total": len(wanted), "layout": lay}

    # Debian：sites-available + sites-enabled 软链
    avail, enabled = lay["avail"], lay["enabled"]
    os.makedirs(avail, exist_ok=True)
    os.makedirs(enabled, exist_ok=True)
    for fname, content in sorted(wanted.items()):
        path = os.path.join(avail, fname)
        old = _read(path)
        if old != content:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            written.append(fname)
        else:
            kept.append(fname)
        link = os.path.join(enabled, fname)
        if not os.path.islink(link) and not os.path.exists(link):
            os.symlink(path, link)
    for fname in sorted(os.listdir(avail)):
        if fname in wanted or not fname.startswith("zpanel-") or not fname.endswith(".conf"):
            continue
        path = os.path.join(avail, fname)
        if _read(path, first_line=True) == MARK:
            link = os.path.join(enabled, fname)
            if os.path.islink(link):
                os.remove(link)
            os.remove(path)
            removed.append(fname)
    return {"written": written, "removed": removed, "kept": kept,
            "total": len(wanted), "layout": lay}


def _read(path: str, first_line: bool = False) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.readline().strip() if first_line else f.read()
    except OSError:
        return ""


# ── 校验 / 重载 ─────────────────────────────────────────
def test() -> dict:
    ctl = _ctl()
    if not ctl:
        return {"ok": False, "output": "apache2ctl/apachectl 未安装"}
    import zkg as _z
    procs = _z.tool("procs")
    r = procs.run([ctl, "-t"], timeout=30, shell=False)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    return {"ok": bool(r.ok), "output": out[:800]}


def reload() -> dict:
    ctl = _ctl()
    if not ctl:
        return {"ok": False, "output": "apache2ctl/apachectl 未安装"}
    import zkg as _z
    procs = _z.tool("procs")
    r = procs.run([ctl, "-k", "graceful"], timeout=30, shell=False)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    return {"ok": bool(r.ok), "output": out[:800],
            "error": "" if r.ok else out[-300:]}


def reload_cmd() -> str:
    return "apachectl -t && apachectl -k graceful"
