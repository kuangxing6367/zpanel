"""引擎生命周期节点：启动 / 停止 / 启动前安全提示 / 依赖自愈 / 运行时长格式化。

以 ``fw`` 为上下文编排各部件；不含状态。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from core.hooks import HookPoints
from core.kernel.banner import emit_banner
from .plugins import load_extensions
from .watchdogs import heartbeat_loop, memory_watchdog_loop

logger = logging.getLogger("zernus")


def format_uptime(fw) -> str:
    """格式化运行时间"""
    seconds = time.time() - fw._start_time if hasattr(fw, "_start_time") else 0
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    parts = []
    if days > 0:
        parts.append(f"{days}天")
    if hours > 0:
        parts.append(f"{hours}小时")
    if mins > 0:
        parts.append(f"{mins}分钟")
    if secs > 0 or not parts:
        parts.append(f"{secs}秒")
    return "".join(parts)


def warn_insecure_config(fw):
    """启动安全提示（协议中立；各接入端自身的令牌提示由适配器注册时给出）"""
    web_cfg = fw.config.get("web", {})
    web_host = web_cfg.get("host", "0.0.0.0")
    if web_host in ("0.0.0.0", "::"):
        logger.warning(
            "⚠ 安全提示: Web 面板监听 0.0.0.0，公网部署请确认已设置访问凭据，"
            "并按需将 web.host 改为 127.0.0.1。"
        )


def auto_heal_plugin_deps(fw):
    """插件依赖自愈：启动时为缺失依赖的插件尝试自动安装。"""
    cfg = fw.config.get("plugin", {})
    auto_install = cfg.get("auto_install_deps_on_startup", True)
    if not auto_install:
        logger.info("插件依赖自愈已关闭 (plugin.auto_install_deps_on_startup: false)")
        return

    with fw.plugin_loader._lock:
        missing_snapshot = {
            name: list(deps)
            for name, deps in fw.plugin_loader._missing_deps.items()
            if deps
        }
    if not missing_snapshot:
        return

    logger.info(
        f"检测到 {len(missing_snapshot)} 个插件依赖缺失，"
        f"启动自愈流程: {list(missing_snapshot.keys())}"
    )
    for plugin_name, deps in missing_snapshot.items():
        try:
            logger.info(f"[{plugin_name}] 自愈：尝试自动安装缺失依赖: {deps}")
            result = fw.plugin_loader.install_missing_deps(plugin_name)
            if result["success"]:
                if result.get("installed"):
                    logger.info(f"[{plugin_name}] 自愈完成，已安装: {', '.join(result['installed'])}")
                else:
                    logger.info(f"[{plugin_name}] 自愈完成，依赖已满足")
            else:
                failed = result.get("failed", [])
                conflicts = result.get("conflicts", [])
                if failed:
                    logger.warning(
                        f"[{plugin_name}] 自愈部分失败，未能安装: "
                        f"{', '.join(failed)}。请在 Web UI 手动处理。"
                    )
                if conflicts:
                    logger.warning(
                        f"[{plugin_name}] 存在版本冲突（不会自动覆盖全局包），"
                        f"请在 Web UI 创建隔离虚拟环境: "
                        f"{', '.join(c['name'] + ' ' + c['required'] + ' (已安装 ' + c['installed'] + ')' for c in conflicts)}"
                    )
        except Exception as e:
            logger.error(f"[{plugin_name}] 依赖自愈异常: {e}")


async def start(fw):
    """启动引擎（异步）"""
    from core.terminal import register_builtins

    fw.loop = asyncio.get_running_loop()
    fw._running = True

    logger.info("=" * 50)
    logger.info("Zeronus 框架 启动中...")
    logger.info("=" * 50)

    warn_insecure_config(fw)

    # 1. 加载官方插件（extensions/）— 必须最先加载，提供基础服务
    load_extensions(fw)

    # 1.5 内核调度器（进程内任务队列：插件 cron / 内置周期任务）
    #     占用 services['scheduler'] 这一内核预留键，必须在扩展加载后、
    #     用户插件注册 cron 任务前就绪。计划任务扩展改用 'scheduler_ext'，
    #     避免用系统级 SchedulerSubsystem 顶掉内核 TaskScheduler（否则
    #     framework.scheduler._jobs / add_plugin_task 全部不可用）。
    try:
        from core.scheduler import TaskScheduler
        _core_sched = TaskScheduler(fw)
        _core_sched.start(fw.loop)
        fw.services.register('scheduler', _core_sched)
        logger.info("内核调度器（TaskScheduler）已注册到 services['scheduler']")
    except Exception as e:
        logger.error(f"内核调度器启动失败: {e}", exc_info=True)

    # 2. 确保 plugins_dat 目录存在
    os.makedirs(fw.plugin_loader.plugins_dat_dir, exist_ok=True)
    fw.plugin_loader.migrate_legacy_configs()

    # 3. 加载用户插件（plugins/）
    loaded = fw.plugin_loader.load_all()
    fw._loaded_user_plugins = loaded
    logger.info(f"已加载 {len(loaded)} 个用户插件: {loaded}")

    # 3.5 插件依赖自愈
    auto_heal_plugin_deps(fw)
    if hasattr(fw.plugin_loader, "_missing_deps"):
        with fw.plugin_loader._lock:
            healed_candidates = list(fw.plugin_loader._missing_deps.keys())
        for plugin_name in healed_candidates:
            if plugin_name not in loaded and fw.plugin_loader.is_plugin_active_in_db(plugin_name):
                if fw.plugin_loader.load_plugin(plugin_name):
                    loaded.append(plugin_name)
        if loaded:
            logger.info(f"自愈后共加载 {len(loaded)} 个插件: {loaded}")

    # 4. 对每个已加载的插件执行 register
    for plugin_name in loaded:
        fw.plugin_loader.register_commands(plugin_name)

    # 4.4 启动内核 Web API（内核自带界面出口：鉴权 + 核心接口 + 前端托管）
    #     放在扩展加载之后：扩展注册的接口可一并挂载；前端产物由内核直接托管。
    try:
        api_cfg = fw.config.get("api") or {}
        if api_cfg.get("enabled", True) is False:
            fw.api_server = None
            logger.info("内核 Web API 已禁用 (api.enabled: false)")
        else:
            from core.api import ApiServer
            fw.api_server = ApiServer(fw)
            fw.api_server.start()
            # 把内核 API 的上下文交给各层：此后 `ctx.register_api(...)` 直接挂到
            # 内核 API 上（不再进缓冲），扩展无需感知底层是谁。
            fw.api_registry = fw.api_server.ctx
    except Exception as e:
        fw.api_server = None
        logger.error(f"内核 Web API 启动失败: {e}", exc_info=True)

    # 4.5 启动内核级多机管理（节点注册表 / 控制面 / 代理）
    #     放在扩展与插件加载之后：此时各层已把数据源与命令处理器注册进来。
    try:
        fw.nodes.start()
    except Exception as e:
        logger.error(f"多机管理启动失败: {e}", exc_info=True)

    # 5. 启动路由表后台刷新
    fw.router.start(fw.loop)
    try:
        await asyncio.to_thread(fw.router._rebuild_routes)
    except Exception as e:
        logger.error(f"路由表预热失败: {e}")

    # 6. 启动统计批量写库器
    fw.stats_writer.start()

    # 6.5 启动内核任务队列（绑定主事件循环，async 任务投递到主循环执行）
    fw.task_queue.start(fw.loop)

    # 7. 启动心跳
    fw._heartbeat_task = asyncio.create_task(heartbeat_loop(fw), name="heartbeat")

    # 8. 启动内存看门狗
    fw._memory_watchdog_task = asyncio.create_task(memory_watchdog_loop(fw), name="memory-watchdog")

    # 9. 触发系统事件
    await fw.event_bus.aemit("system.plugin.loaded", {"plugins": loaded})

    # 9.5 触发启动扩展点
    try:
        await fw.hooks.trigger_async(HookPoints.LIFECYCLE_STARTUP)
    except Exception as e:
        logger.error(f"启动扩展点异常: {e}", exc_info=True)

    # 10. 终端命令注册 + 交互输入启动
    register_builtins(fw)
    fw.terminal.start()

    # 10.5/10.6 WebSocket / gRPC 启动已迁出内核：由 webui 扩展在 register()
    # 内按 config.ws / config.grpc 自行拉起（内核不再硬编码 Web 传输栈）。

    emit_banner(
        version=fw._read_version(),
        project_root=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        core_loaded=getattr(fw, "_loaded_extensions", []),
        user_loaded=getattr(fw, "_loaded_user_plugins", []),
        config=fw.config,
        logger=logger,
    )


async def stop(fw):
    """停止引擎（异步）"""
    logger.info("正在停止框架...")
    fw._running = False

    try:
        await fw.hooks.trigger_async(HookPoints.LIFECYCLE_SHUTDOWN)
    except Exception as e:
        logger.warning(f"关闭扩展点异常: {e}")

    fw.terminal.stop()

    try:
        if getattr(fw, "api_server", None) is not None:
            fw.api_server.stop()
            fw.api_server = None
    except Exception as e:
        logger.warning(f"内核 Web API 停止异常: {e}")

    try:
        fw.nodes.stop()
    except Exception as e:
        logger.warning(f"多机管理停止异常: {e}")

    try:
        await asyncio.to_thread(fw.task_queue.stop)
    except Exception as e:
        logger.warning(f"任务队列停止异常: {e}")

    try:
        await fw.stats_writer.stop()
    except Exception as e:
        logger.warning(f"统计写库器停止异常: {e}")

    try:
        await fw.router.stop()
    except Exception as e:
        logger.warning(f"路由表刷新任务停止异常: {e}")

    if fw._heartbeat_task:
        fw._heartbeat_task.cancel()
        try:
            await fw._heartbeat_task
        except (asyncio.CancelledError, Exception):
            pass
        fw._heartbeat_task = None

    if fw._memory_watchdog_task:
        fw._memory_watchdog_task.cancel()
        try:
            await fw._memory_watchdog_task
        except (asyncio.CancelledError, Exception):
            pass
        fw._memory_watchdog_task = None

    for name in ("ws_server", "web_server", "scheduler"):
        svc = fw.services.get(name)
        if svc is not None:
            try:
                if asyncio.iscoroutinefunction(svc.stop):
                    await svc.stop()
                else:
                    svc.stop()
            except Exception as e:
                logger.warning(f"服务 [{name}] 停止异常: {e}")

    # gRPC 服务优雅关闭（grpc.server.stop 需要 grace 参数）
    grpc_srv = getattr(fw, "_grpc_server", None)
    if grpc_srv is not None:
        try:
            grpc_srv.stop(0)
        except Exception as e:
            logger.warning(f"gRPC 服务关闭异常: {e}")
        fw._grpc_server = None

    try:
        fw._db_executor.shutdown(wait=False)
    except Exception as e:
        logger.warning(f"数据库线程池关闭异常: {e}")

    await fw.event_bus.aemit("system.plugin.unloaded", {})
    logger.info("框架已停止")
