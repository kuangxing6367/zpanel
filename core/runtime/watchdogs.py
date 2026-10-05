"""后台看门狗节点：插件注册心跳 + 内存监控。

两个循环均以 ``fw`` 为上下文，读到 ``fw._running`` 为 False 时退出。
"""
from __future__ import annotations

import asyncio
import gc
import logging

logger = logging.getLogger("zernus")


async def heartbeat_loop(fw):
    """插件注册心跳：周期性检查插件文件变更并重新注册（不阻塞事件循环）"""
    while fw._running:
        try:
            await asyncio.sleep(fw._heartbeat_interval)
            if not fw._running:
                break
            await asyncio.to_thread(fw.plugin_loader.heartbeat_register)
            # 每分钟自检：清理不应存在的孤儿任务/命令（插件已删除时自动校正）
            await asyncio.to_thread(fw.plugin_loader.self_check_orphans)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"插件注册心跳异常: {e}")


async def memory_watchdog_loop(fw):
    """内存看门狗：周期检查 RSS，超限时清理框架级缓存并强制 GC

    psutil 为可选依赖：缺失时跳过 RSS 采样，仍执行缓存清理与 GC。
    """
    try:
        import psutil
        process = psutil.Process()
        has_psutil = True
    except ImportError:
        process = None
        has_psutil = False
        logger.warning("[内存看门狗] 未安装 psutil，跳过 RSS 采样（仍执行缓存清理/GC）")

    while fw._running:
        try:
            await asyncio.sleep(fw._memory_check_interval)
            if not fw._running:
                break
            rss_mb = None
            if has_psutil and process is not None:
                rss_mb = process.memory_info().rss / 1024 / 1024
            if rss_mb is not None and rss_mb <= fw._memory_limit_mb:
                continue
            if rss_mb is None:
                logger.debug("[内存看门狗] psutil 不可用，仅执行缓存清理与 GC")
            else:
                logger.warning(
                    f"[内存看门狗] RSS {rss_mb:.1f}MB 超过限制 {fw._memory_limit_mb}MB，触发清理"
                )
            # 1. 清理框架级角色缓存
            from core.messaging.event import _user_role_cache, _group_role_cache
            cache_before = len(_user_role_cache) + len(_group_role_cache)
            _user_role_cache.clear()
            _group_role_cache.clear()
            # 2. 清理 stats_writer 聚合计数（高频但不关键）
            if hasattr(fw, "stats_writer"):
                fw.stats_writer._cmd_hits.clear()
                fw.stats_writer._kw_hits.clear()
            # 3. 强制 GC 回收循环引用
            collected = gc.collect()
            if rss_mb is not None and process is not None:
                after_rss = process.memory_info().rss / 1024 / 1024
                logger.info(
                    f"[内存看门狗] 清理完成：缓存 {cache_before} 条，GC 回收 {collected} 对象，"
                    f"RSS {rss_mb:.1f}→{after_rss:.1f}MB（节省 {rss_mb - after_rss:.1f}MB）"
                )
            else:
                logger.info(
                    f"[内存看门狗] 清理完成：缓存 {cache_before} 条，GC 回收 {collected} 对象"
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[内存看门狗] 异常: {e}")
