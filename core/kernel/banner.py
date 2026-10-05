"""启动横幅渲染（内核细模块，纯函数，无框架依赖）。

渲染逻辑与 I/O 分离：``render_banner`` 只算出行列表，由调用方决定如何输出
（默认 ``emit_banner`` 用传入的 logger 逐行 info）。
"""

from __future__ import annotations

import os
from typing import List


def read_version(project_root: str) -> str:
    """读取 VERSION 文件，供启动横幅显示。"""
    try:
        vp = os.path.join(project_root, "VERSION")
        with open(vp, "r", encoding="utf-8") as f:
            return (f.read().strip() or "?")
    except Exception:
        return "?"


def render_banner(*, version: str, project_root: str, core_loaded: list,
                  user_loaded: list, config: dict) -> List[str]:
    """返回横幅行列表（不参与 I/O）。"""
    project_name = (config.get("project") or {}).get("name") or "ZER NUS"
    # VERSION 文件常带 v 前缀（与 git tag 一致）；此处统一归一，避免显示成 vv0.0.1
    version = (version or "?").lstrip("vV") or "?"

    db_cfg = config.get("database", {})
    db_type = db_cfg.get("type", "sqlite")
    if db_type == "sqlite":
        db_desc = f"SQLite → {db_cfg.get('path', 'data/zernus.db')}"
    else:
        db_desc = f"{db_type} → {db_cfg.get('host')}:{db_cfg.get('port')}/{db_cfg.get('database')}"

    endpoints = []
    if "onebot_adapter" in core_loaded:
        ob = config.get("onebot", {})
        endpoints.append(
            f"OneBot WS : {ob.get('listen_host', '0.0.0.0')}:{ob.get('listen_port', 6830)}")
    if "webui" in core_loaded:
        web = config.get("web", {})
        endpoints.append(
            f"WebUI      : http://{web.get('host', '127.0.0.1')}:{web.get('port', 8080)}")
    if "http_api" in core_loaded:
        ha = config.get("http_api", {})
        if ha.get("enabled"):
            endpoints.append(
                f"HTTP API   : http://{ha.get('host', '127.0.0.1')}:{ha.get('port', 1145)}")

    lines = [
        "=" * 60,
        f" {project_name} v{version}",
        "=" * 60,
        f" 数据目录 : {os.path.join(project_root, 'data')}",
        f" 数据库   : {db_desc}",
        f" 官方扩展 : {len(core_loaded)} 个 → {', '.join(core_loaded) if core_loaded else '(无)'}",
        f" 用户插件 : {len(user_loaded)} 个 → {', '.join(user_loaded) if user_loaded else '(无)'}",
    ]
    if endpoints:
        lines.append(" 监听端口 :")
        for ep in endpoints:
            lines.append(f"   - {ep}")
    lines.append("=" * 60)
    return lines


def emit_banner(*, version: str, project_root: str,
                core_loaded: list, user_loaded: list, config: dict,
                logger) -> None:
    """渲染并逐行输出到 logger。"""
    for ln in render_banner(
        version=version, project_root=project_root,
        core_loaded=core_loaded, user_loaded=user_loaded, config=config,
    ):
        logger.info(ln)
