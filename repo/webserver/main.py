# -*- coding: utf-8 -*-
"""
webserver — Web 服务器适配器门面（机制包，零框架依赖）

**建站是顶层概念，web 服务器是底层可替换的实现。** 本包定义适配器契约，
框架/扩展只面向契约编程，不认识 nginx/Apache 的任何细节 —— 与
svcmgr（服务管理）、pkg（包管理器）同一套门面模式。

契约（每个 webserver_* 实现包必须实现）：

    applies()                    本机是否装了这个引擎
    detect()                     {id,name,available,exe,version,layout,capabilities}
                                 capabilities: {static,php,proxy,ssl} 如实声明
    render(sites)                {files: {文件名: 内容}}   纯渲染（预览用），不落盘
    deploy(sites, conf_dir)      渲染 + 落盘 + 清理自家托管文件
                                 → {written,removed,kept,total}
                                 sites = 扩展给的站点描述（to_site_spec 字段）；
                                 conf_dir 是调用方指定的托管目录，后端若用
                                 发行版自有布局（如 Debian sites-available）
                                 可用自己的目录并在 detect().layout 里如实报告
    test()                       {ok, output}    语法校验（nginx -t / apachectl -t）
    reload()                     {ok, output}    平滑重载
    reload_cmd()                 展示用

统一原则：
- 渲染出的配置必须带托管标记（'# managed by zpanel'），清理只碰自家文件；
- capabilities 不支持的能力（如 MVP 的 apache+ssl）要如实说，调用方负责提示；
- test 不过就不 reload —— 调用方（sites 扩展）负责这个门禁。
"""
from __future__ import annotations

import platform

import zkg

__version__ = "1.0.0"

# 按优先级登记实现包：auto 选择时先命中谁用谁
IMPLS = ("webserver_nginx", "webserver_apache")


class NoBackend(RuntimeError):
    """本机没有可用的 web 服务器引擎。"""


def _mods() -> list:
    """所有已加载且适用的实现包模块（按 IMPLS 优先级）。"""
    out = []
    for tid in IMPLS:
        mod = zkg.tool(tid)
        if mod is None:
            continue
        try:
            if mod.applies():
                out.append(mod)
        except Exception:
            continue
    return out


def available() -> list:
    """本机所有可用的引擎（探测全量，供 UI 如实展示与选择）。"""
    return [m.detect() for m in _mods()]


def select(pref: str = "auto") -> dict:
    """选定当前生效引擎。pref = 'auto' 或 adapter_id。

    返回 {'active': {...detect...}, 'available': [...]}；一个都没有时
    active 为 None —— 调用方（sites）据此回退到「仅生成配置」模式。
    """
    mods = _mods()
    detected = [m.detect() for m in mods]
    active = None
    if pref and pref != "auto":
        for m, d in zip(mods, detected):
            if m.adapter_id == pref:
                active = d
                break
    if active is None and detected:
        active = detected[0]
    return {"active": active, "available": detected}


def impl_for(adapter_id: str):
    """取指定 id 的实现模块；没有则返回 None。"""
    for m in _mods():
        if m.adapter_id == adapter_id:
            return m
    return None


def active_impl(pref: str = "auto"):
    """select().active 对应的实现模块；无可用引擎时抛 NoBackend。"""
    sel = select(pref)
    if sel["active"] is None:
        raise NoBackend(
            f"本机没有可用的 web 服务器引擎（探测过：{', '.join(IMPLS)}）")
    return impl_for(sel["active"]["id"])


def platforms() -> list:
    """各实现包装载/适用情况（排障用）。"""
    out = []
    for tid in IMPLS:
        mod = zkg.tool(tid)
        if mod is None:
            out.append({"id": tid, "loaded": False, "applies": False})
            continue
        try:
            out.append({"id": tid, "loaded": True, "applies": bool(mod.applies())})
        except Exception as e:
            out.append({"id": tid, "loaded": True, "applies": False,
                        "error": f"{type(e).__name__}: {e}"})
    return out
