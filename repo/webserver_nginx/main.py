# -*- coding: utf-8 -*-
"""webserver_nginx — Nginx 适配器（webserver 契约的实现）。

**渲染/落盘/清理不在这里实现** —— 全部委托 ngxconf 机制包（nginx 配置的
实现只此一份），本包只做契约翻译：detect / render / deploy / test / reload。
"""
import shutil

import zkg

__version__ = "1.0.0"

adapter_id = "nginx"
name = "Nginx"

_NGINX_PATHS = "/usr/sbin:/usr/local/nginx/sbin"


def _ngx():
    mod = zkg.tool("ngxconf")
    if mod is None:
        raise RuntimeError("webserver_nginx 依赖 ngxconf 机制包")
    return mod


def _probe():
    mod = zkg.tool("probe")
    if mod is None:
        raise RuntimeError("webserver_nginx 依赖 probe 机制包")
    return mod


def _exe() -> str:
    return shutil.which("nginx", path=_NGINX_PATHS) or ""


def applies() -> bool:
    return bool(_probe().which("nginx", extra_paths=("/usr/sbin", "/usr/local/nginx/sbin")))


def detect() -> dict:
    info = _probe().probe("nginx", exes=["nginx"], args=("-v",),
                          extra_paths=("/usr/sbin", "/usr/local/nginx/sbin"))
    exe = info.get("exe") or ""
    return {
        "id": adapter_id, "name": name,
        "available": bool(exe), "exe": exe,
        "version": str(info.get("version") or ""),
        "layout": {"conf_dir": "由调用方指定（sites.conf_dir）"},
        "capabilities": {"static": True, "php": True, "proxy": True, "ssl": True},
    }


def render(sites: list) -> dict:
    """纯渲染（预览用）：{文件名: 内容}。"""
    return _ngx().render_all(sites)


def deploy(sites: list, conf_dir: str) -> dict:
    """渲染 + 落盘 + 清理自家托管文件（ngxconf.dump 一体完成）。"""
    return _ngx().dump(conf_dir, sites)


def test() -> dict:
    exe = _exe()
    if not exe:
        return {"ok": False, "output": "nginx 未安装"}
    return _ngx().test(exe)


def reload() -> dict:
    exe = _exe()
    if not exe:
        return {"ok": False, "output": "nginx 未安装"}
    return _ngx().reload(exe)


def reload_cmd() -> str:
    return "nginx -t && nginx -s reload"
