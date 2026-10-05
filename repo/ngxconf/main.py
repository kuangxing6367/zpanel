"""ngxconf —— Nginx 配置渲染与重载（机制包）。

面板管站点的正确姿势：**管配置，不管进程**。
本包只做三件事：把站点描述渲染成 server 块、把托管文件落盘/清理、让 nginx 重载。
Nginx 的安装、开机自启、主配置（nginx.conf）不属于本包职责。

稳定 API 表面：
    render_server(site) -> str               渲染单个站点为 server 块文本
    render_all(sites) -> dict                 {filename: content}
    dump(conf_dir, sites) -> dict             落盘并清理已删除站点的托管文件
    test(exe, *, conf=None, prefix=None) -> dict      nginx -t
    reload(exe, *, prefix=None) -> dict               nginx -s reload
    find_exe(*, extra_paths=()) -> str | None

站点 dict 字段（与 vhost 机制包共用同一套描述，便于两个后端互切）：
    id, name, domains[], kind('static'|'php'|'proxy'), root_dir, index,
    target, php_socket, ssl{enabled, cert, key}, extra

托管约定：
- 生成的文件首行固定为 ``# managed by zpanel``；
  dump() **只**删除首行带该标记、且当前已无对应站点的 ``*.conf``，
  绝不碰人手写的配置 —— 这是「面板不越权」的底线。
- 落盘用 lock 机制包的命名锁串行化，避免并发写坏文件。
"""
from __future__ import annotations

import os
import re

import zkg                                  # 由 zkg loader 注入

MARKER = "# managed by zpanel"
_CONF_RE = re.compile(r"^[A-Za-z0-9_.-]+\.conf$")


def _deps():
    lock = zkg.tool("lock")
    procs = zkg.tool("procs")
    if lock is None or procs is None:
        raise RuntimeError("ngxconf 依赖 lock / procs 机制包，但未被加载（检查 manifest.dependencies）")
    return lock, procs


def _q(path) -> str:
    """Nginx 配置里的路径统一用双引号包住（Windows 反斜杠 / 空格路径都能吃）。"""
    return '"' + str(path).replace("\\", "/").replace('"', '') + '"'


def _dedupe_domains(domains) -> list:
    out, seen = [], set()
    for d in domains or []:
        d = str(d).strip().lower()
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


# ── 渲染 ────────────────────────────────────────────────
def _ssl_block(site, ssl) -> str:
    lines = ["    listen 443 ssl;", "    http2 on;"]
    if ssl.get("cert"):
        lines.append(f"    ssl_certificate     {_q(ssl['cert'])};")
    if ssl.get("key"):
        lines.append(f"    ssl_certificate_key {_q(ssl['key'])};")
    lines += [
        "    ssl_protocols       TLSv1.2 TLSv1.3;",
        "    ssl_ciphers         HIGH:!aNULL:!MD5;",
        "    ssl_session_cache   shared:SSL:10m;",
        "    ssl_session_timeout 10m;",
    ]
    return "\n".join(lines)


def _static_location(site) -> str:
    return "\n".join([
        "    location / {",
        "        try_files $uri $uri/ =404;",
        "    }",
    ])


def _php_location(site) -> str:
    upstream = site.get("php_socket") or "127.0.0.1:9000"
    if str(upstream).startswith("/") or "unix:" in str(upstream):
        pass_expr = "unix:" + str(upstream).replace("unix:", "")
    else:
        pass_expr = str(upstream)
    return "\n".join([
        "    location / {",
        "        try_files $uri $uri/ /index.php?$query_string;",
        "    }",
        "",
        "    location ~ \\.php$ {",
        "        try_files $uri =404;",
        f"        fastcgi_pass {pass_expr};",
        "        fastcgi_index index.php;",
        '        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;',
        "        include fastcgi_params;",
        "    }",
    ])


def _proxy_location(site) -> str:
    target = str(site.get("target") or "").strip()
    if not target:
        target = "127.0.0.1:8080"
    if not target.startswith(("http://", "https://")):
        target = "http://" + target
    return "\n".join([
        "    location / {",
        f"        proxy_pass {target};",
        "        proxy_http_version 1.1;",
        "        proxy_set_header Host              $host;",
        "        proxy_set_header X-Real-IP         $remote_addr;",
        "        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;",
        "        proxy_set_header X-Forwarded-Proto $scheme;",
        "        proxy_set_header Upgrade           $http_upgrade;",
        '        proxy_set_header Connection        "upgrade";',
        "        proxy_read_timeout 300s;",
        "        proxy_send_timeout 300s;",
        "    }",
    ])


def render_server(site: dict) -> str:
    """把一个站点 dict 渲染成完整的 server 块文本。"""
    site = dict(site or {})
    sid = str(site.get("id") or "site")
    name = str(site.get("name") or sid)
    domains = _dedupe_domains(site.get("domains")) or ["_"]
    kind = str(site.get("kind") or "static").lower()
    root_dir = site.get("root_dir") or ""
    index = site.get("index") or "index.html index.htm"
    ssl = site.get("ssl") or {}
    has_ssl = bool(ssl.get("enabled") and (ssl.get("cert") or ssl.get("key")))

    head = [MARKER, f"# site: {name} ({sid})", "server {"]
    listen = ["    listen 80;", "    listen [::]:80;"]
    body = [f"    server_name {' '.join(domains)};"]
    if root_dir:
        body.append(f"    root  {_q(root_dir)};")
        body.append(f"    index {index};")
    log_dir = str(site.get("log_dir") or "").strip()
    if log_dir:
        log_dir = log_dir.replace("\\", "/")
        body.append(f"    access_log {_q(f'{log_dir}/{sid}.access.log')};")
        body.append(f"    error_log  {_q(f'{log_dir}/{sid}.error.log')};")
    if has_ssl:
        body.append(_ssl_block(site, ssl))

    if kind == "proxy":
        body.append(_proxy_location(site))
    elif kind == "php":
        body.append(_php_location(site))
    else:
        body.append(_static_location(site))

    text = "\n".join(head + listen + body + ["}", ""])
    if has_ssl:
        text += "\n".join([
            MARKER, f"# site: {name} ({sid}) — http -> https",
            "server {",
            "    listen 80;",
            "    listen [::]:80;",
            f"    server_name {' '.join(domains)};",
            "    return 301 https://$host$request_uri;",
            "}", "",
        ])
    extra = str(site.get("extra") or "").strip()
    if extra:
        text += "\n" + extra + "\n"
    return text


def render_all(sites) -> dict:
    """{文件名: 内容}；文件名取 ``<id>.conf``（id 先做安全过滤）。"""
    out = {}
    for s in sites or []:
        sid = re.sub(r"[^A-Za-z0-9_.-]", "-", str((s or {}).get("id") or "")).strip("-")
        if not sid:
            continue
        out[f"{sid}.conf"] = render_server(s)
    return out


# ── 落盘 ────────────────────────────────────────────────
def dump(conf_dir, sites) -> dict:
    """把站点配置写入 conf_dir，并清理已无对应站点的**托管**文件。"""
    lock, _ = _deps()
    conf_dir = os.path.abspath(str(conf_dir))
    os.makedirs(conf_dir, exist_ok=True)
    wanted = render_all(sites)

    written, removed, kept = [], [], []
    with lock.named("ngxconf.dump"):
        for fname, content in sorted(wanted.items()):
            path = os.path.join(conf_dir, fname)
            old = None
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        old = f.read()
                except Exception:
                    old = None
            if old != content:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                written.append(fname)

        for fname in sorted(os.listdir(conf_dir)):
            if fname in wanted or not _CONF_RE.match(fname):
                continue
            path = os.path.join(conf_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    head = f.readline().strip()
            except Exception:
                continue
            if head == MARKER:                  # 只删自己生成的
                try:
                    os.remove(path)
                    removed.append(fname)
                except Exception:
                    pass
            else:
                kept.append(fname)              # 人手写的配置：不碰
    return {"ok": True, "conf_dir": conf_dir, "written": written,
            "removed": removed, "untouched": kept, "total": len(wanted)}


# ── Nginx 交互 ──────────────────────────────────────────
def find_exe(*, extra_paths=()) -> str | None:
    _, procs = _deps()
    for name in ("nginx", "nginx.exe"):
        found = procs.which(name)
        if found:
            return found
    for d in extra_paths or ():
        for name in ("nginx", "nginx.exe", os.path.join("sbin", "nginx"),
                     os.path.join("sbin", "nginx.exe")):
            cand = os.path.join(d, name)
            if os.path.isfile(cand):
                return os.path.abspath(cand)
    return None


def test(exe: str, *, conf: str = None, prefix: str = None, timeout: float = 15.0) -> dict:
    """``nginx -t``：校验配置。返回 {ok, output, returncode}。"""
    _, procs = _deps()
    argv = [exe, "-t"]
    if conf:
        argv += ["-c", str(conf)]
    if prefix:
        argv += ["-p", str(prefix)]
    res = procs.run(argv, timeout=timeout)
    return {"ok": res.ok, "output": (res.stdout or res.stderr).strip(),
            "returncode": res.returncode}


def reload(exe: str, *, prefix: str = None, timeout: float = 15.0) -> dict:
    """``nginx -s reload``：平滑重载。"""
    _, procs = _deps()
    argv = [exe, "-s", "reload"]
    if prefix:
        argv += ["-p", str(prefix)]
    res = procs.run(argv, timeout=timeout)
    return {"ok": res.ok, "output": (res.stdout or res.stderr).strip(),
            "returncode": res.returncode}
