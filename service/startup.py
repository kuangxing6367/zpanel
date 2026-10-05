"""服务级（Service Layer）：sys 服务与 user 服务的拉起与生命周期。

对应手写笔记：
- 服务级职责：zkg 包管理、框架服务基础（mg/db 等）、软件启动与注销核心服务、看门狗。
- 启动流程：拉起 sys 服务（初始化）→ 拉起 user 服务；两者均监听本地端口。

本模块是「服务级」的程序化入口，main.py 在拉起内核（core）后调用这里。
"""
import asyncio
import logging
import os

from core.kernel.paths import project_root
from service.watchdog import Watchdog

logger = logging.getLogger('zernus')


def _watchdog_limit_mb(config: dict, default: int = 256) -> int:
    """内存上限统一取自 service.watchdog.max_memory_mb（与服务级看门狗同源）。"""
    wd = ((config.get('service') or {}).get('watchdog') or {})
    try:
        return int(wd.get('max_memory_mb', default))
    except (TypeError, ValueError):
        return default


def _watchdog_interval_s(config: dict, default: float = 30.0) -> float:
    """采样间隔取自 service.watchdog.interval（与服务级看门狗同源）。"""
    wd = ((config.get('service') or {}).get('watchdog') or {})
    try:
        return float(wd.get('interval', default))
    except (TypeError, ValueError):
        return default


def open_local_port(host: str, port: int, label: str):
    """在内核事件循环里监听一个本地端口（服务级控制/状态通道），返回 Task。"""
    async def _handler(reader, writer):
        try:
            await asyncio.wait_for(reader.read(256), timeout=2)
            writer.write(b'OK\n')
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _serve():
        server = await asyncio.start_server(_handler, host, port)
        logger.info(f"[{label}] 监听本地端口 {host}:{port}")
        async with server:
            await server.serve_forever()

    return asyncio.ensure_future(_serve())


def _zkg_sources(config: dict) -> list:
    """构造 zkg 源列表：默认仅本地；配置 zkg.official_source 后启用远程官方源。

    注意：内置默认源里 http 源是 enabled=True，但按笔记「默认不主动连接远程」，
    未显式配置 official_source 时这里把它关掉，避免启动即联网。
    """
    from service.zkg import defaults
    srcs = defaults.get_default_sources()
    official = ((config.get('zkg') or {}).get('official_source') or '').strip()
    for s in srcs:
        if s.get('type') == 'http':
            if official:
                s['url'] = official
            else:
                s['enabled'] = False
    return srcs


async def start_sys_service(fw, config: dict):
    """拉起 sys 服务（初始化）：zkg 包管理、框架服务（mg/db）、看门狗。"""
    svc = (config.get('service', {}) or {}).get('sys', {}) or {}
    host = svc.get('host', '127.0.0.1')
    port = int(svc.get('port', 38001))

    # zkg 包管理：scan(本地仓库 + 软件层 manifest) → resolve(依赖图) → 重建 data/plugins.db
    #            → 按依赖加载机制包。**依赖声明来自软件层**：
    #            扩展在 software/extensions/<name>/manifest.toml 的 dependencies 里
    #            声明它需要哪些机制包，zkg 据此决定加载（零依赖的包不加载）。
    try:
        from service.zkg.loader import Loader
        from core.ctx import PLUGIN_API_VERSION
        root = (fw.config.get('zkg') or {}).get('local_dir', 'repo')
        if not os.path.isabs(root):
            root = os.path.join(project_root(), root)
        # 扫描根：软件层的两处 —— 官方扩展（能力实现）与用户插件（业务应用）
        scan_roots = [
            os.path.join(project_root(), 'software', 'extensions'),
            os.path.join(project_root(), 'software', 'plugins'),
        ]
        data_dir = os.path.join(project_root(), 'data')
        loader = Loader(root, data_dir,
                        sources_cfg=_zkg_sources(fw.config),
                        scan_roots=scan_roots,
                        plugin_api_version=PLUGIN_API_VERSION)
        result = loader.run()
        # 机制包统一暴露：扩展经 ctx.zkg_tool(name) 取用；包之间经顶层 `zkg` 取用
        fw.zkg_tools = loader.loaded_tools()
        # 加载全貌（供面板观测：哪些包因无人依赖被剪枝、哪些依赖缺失）
        fw.zkg_info = {
            'repo': root,
            'scan_roots': scan_roots,
            'sources': [s.id for s in loader.registry.enabled()],
            'stats': result.get('stats') or {},
            'loaded': result.get('loaded_tools') or [],
            'skipped': result.get('skipped_tools') or [],
            'missing': result.get('missing') or [],
            'manifests': result.get('plugin_count', 0),
            'api_incompatible': result.get('api_incompatible') or [],
        }
        tools = result.get('loaded_tools') or []
        logger.info(
            f"[sys] zkg 包管理就绪：仓库 {result.get('plugin_count', 0)} 个 manifest，"
            f"按依赖加载机制包 {len(tools)} 个"
            + (f" {tools}" if tools else "")
            + (f"（剪枝 {result.get('skipped_tools')}）" if result.get('skipped_tools') else "")
            + (f"（缺失依赖 {result.get('missing')}）" if result.get('missing') else "")
            + f"（仓库目录 {root}）"
        )
    except Exception as e:
        logger.warning(f"[sys] zkg 初始化跳过: {e}")

    # 框架服务基础（mg=消息网关 / db=数据库）已在内核初始化阶段就绪
    logger.info("[sys] 框架服务（mg/db）已就绪")

    wd = Watchdog(limit_mb=_watchdog_limit_mb(config),
                  interval=_watchdog_interval_s(config))
    wd.start()
    task = open_local_port(host, port, 'sys')
    logger.info("[sys] sys 服务已拉起（初始化完成）")
    return {'name': 'sys', 'task': task, 'watchdog': wd, 'port': port}


async def start_user_service(fw, config: dict):
    """拉起 user 服务：加载软件级（extensions/plugins）并启动；看门狗。"""
    svc = (config.get('service', {}) or {}).get('user', {}) or {}
    host = svc.get('host', '127.0.0.1')
    port = int(svc.get('user_port', 38002))

    # 软件级：经内核加载用户插件与官方扩展（WebUI / OneBot 等在此启动）
    await fw.start()

    wd = Watchdog(limit_mb=_watchdog_limit_mb(config),
                  interval=_watchdog_interval_s(config))
    wd.start()
    task = open_local_port(host, port, 'user')
    logger.info("[user] user 服务已拉起（软件级已启动）")
    return {'name': 'user', 'task': task, 'watchdog': wd, 'port': port}
