"""zkg 命令行工具（脚手架）。

用法（在项目根目录）：

    python -m service.zkg new <name> [--deps store,ws] [--desc 描述]

在 ``software/plugins/<name>/`` 生成插件骨架：

    manifest.toml     zkg 包清单（dependencies 声明所需官方机制包，
                      zkg 启动时据此按需加载并暴露给 ctx.zkg_tool）
    main.py           register(ctx) 入口，含最小命令注册示例
    requirements.txt  Python 依赖（pip 安装清单，默认空）

纯标准库，零第三方依赖。
"""
from __future__ import annotations

import argparse
import os
import re
import sys

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

_MAIN_TEMPLATE = '''\
"""{name} —— {desc}

由 `zkg new` 生成。register(ctx) 是框架加载插件的唯一入口，
所有注册都写在这个函数里；不要在 register 之外执行副作用。
"""


def register(ctx):
    log = ctx.logger

    # 示例：正则命令（收到匹配消息时回复）。pattern / 优先级 / 权限见 docs/writing-plugins.md
    @ctx.command(r"^{name}$", handler=_hello)
    def _hello(event):
        return "{name} 就绪。"

    # 示例：消费 zkg 官方机制包 —— 先在 manifest.toml 的 dependencies 里声明，
    # 再用 ctx.zkg_tool("<工具id>") 取用（未声明/未加载时返回 None）。
{tool_example}
    log.info("{name} 已注册")
'''

_TOOL_EXAMPLE_WITH = '''\
    store = ctx.zkg_tool("store")
    if store is not None:
        kv = store.KV(ctx.get_data_dir() + "/data.json")
        # kv.set("k", "v") / kv.get("k") —— 统一 KV，原子写，别再手搓 json.load/dump
'''

_TOOL_EXAMPLE_NONE = '''\
    # store = ctx.zkg_tool("store")
    # if store is not None:
    #     kv = store.KV(ctx.get_data_dir() + "/data.json")
'''

_MANIFEST_TEMPLATE = '''\
# zkg 包清单：dependencies 声明所需官方机制包（id 见 repo/ 或官方源索引），
# zkg 启动时解析并按需加载，插件内经 ctx.zkg_tool("<id>") 取用。
# api_version：兼容的插件 API 主版本（ctx.api_version），区间写法如 ">=1,<2"。
[package]
id = "{name}"
name = "{name}"
type = "plugin"
version = "0.1.0"
description = "{desc}"
dependencies = [{deps}]
api_version = "1"
'''

_REQ_TEMPLATE = '''\
# Python 第三方依赖（pip 安装清单），每行一个，如：
# requests>=2.31
'''


def _write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def new(name: str, deps: list = None, desc: str = "",
        plugins_dir: str = None) -> str:
    """生成插件骨架，返回插件目录绝对路径。

    :param name: 插件 id（小写字母开头，小写字母/数字/下划线）
    :param deps: 依赖的官方机制包 id 列表（写进 manifest.toml）
    :param desc: 描述（同时写进 manifest 与 main.py 文档串）
    :param plugins_dir: 插件根目录，默认 ``<项目根>/software/plugins``
    :raises FileExistsError: 目标目录已存在
    :raises ValueError: 名称不合法
    """
    if not _NAME_RE.match(name):
        raise ValueError(
            f"插件名 {name!r} 不合法：需小写字母开头，仅含小写字母/数字/下划线，2~64 位")
    if plugins_dir is None:
        from .defaults import PROJECT_ROOT
        plugins_dir = os.path.join(PROJECT_ROOT, "software", "plugins")
    target = os.path.join(plugins_dir, name)
    if os.path.exists(target):
        raise FileExistsError(f"目录已存在: {target}")

    os.makedirs(target, exist_ok=True)
    deps = [d.strip() for d in (deps or []) if d and d.strip()]
    tool_example = _TOOL_EXAMPLE_WITH if "store" in deps else _TOOL_EXAMPLE_NONE
    desc = desc or f"{name} 插件"

    _write(os.path.join(target, "main.py"),
           _MAIN_TEMPLATE.format(name=name, desc=desc, tool_example=tool_example))
    _write(os.path.join(target, "manifest.toml"),
           _MANIFEST_TEMPLATE.format(
               name=name, desc=desc,
               deps=", ".join(f'"{d}"' for d in deps)))
    _write(os.path.join(target, "requirements.txt"), _REQ_TEMPLATE)
    return target


def main(argv: list = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m service.zkg", description="zkg 包管理命令行")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_new = sub.add_parser("new", help="生成插件骨架（software/plugins/<name>/）")
    p_new.add_argument("name", help="插件 id（小写字母开头）")
    p_new.add_argument("--deps", default="",
                       help="依赖的官方机制包 id，逗号分隔（如 store,ws）")
    p_new.add_argument("--desc", default="", help="插件描述")
    p_new.add_argument("--dir", default=None,
                       help="插件根目录（默认 software/plugins）")

    args = parser.parse_args(argv)
    if args.cmd == "new":
        try:
            target = new(args.name, deps=args.deps.split(","),
                         desc=args.desc, plugins_dir=args.dir)
        except (ValueError, FileExistsError) as e:
            print(f"[zkg] 失败: {e}")
            return 1
        print(f"[zkg] 插件骨架已生成: {target}")
        print("      下一步: 编辑 main.py 的 register(ctx)，manifest.toml 声明机制包依赖")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
