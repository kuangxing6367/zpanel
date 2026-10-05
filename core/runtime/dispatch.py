"""事件分发节点：协议适配器入口 → 内核。

职责：把适配器转换后的内部事件分发到事件总线 / 路由器 / 原始消息处理器。
每个函数接受引擎实例 ``fw`` 作为上下文，不在模块内保存状态。
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("zernus")

from core.log_broker import log_broker
from core.hooks import HookPoints


async def dispatch_event(fw, event: dict):
    """协议适配器入口：将转换后的内部事件分发到框架。

    fw: 引擎实例（提供 hooks / event_bus / config / stats_writer / router）
    """
    event_type = event.get("type", "")
    bot_name = event.get("bot_name", "default")

    # 事件进入内核前的扩展点（任何 handler 返回 False 即丢弃该事件）
    try:
        if False in (await fw.hooks.trigger_async(
                HookPoints.EVENT_BEFORE_DISPATCH, event, bot_name)):
            logger.debug("事件被扩展点 event.before_dispatch 拦截丢弃")
            return
    except Exception as e:
        logger.error(f"event.before_dispatch 扩展点异常: {e}")

    try:
        logger.debug(f"dispatch_event: type={event_type} bot={bot_name} msg_type={event.get('message_type','')}")

        # 元事件 → 广播
        if event_type == "meta_event":
            meta_type = event.get("sub_type", "unknown")
            await fw.event_bus.aemit(f"meta.{meta_type}", event)
            return

        # 消息事件 → 路由
        if event_type == "message":
            if await dispatch_raw_message_handlers(fw, event, bot_name):
                return

            from core.messaging.event import _extract_text
            raw_message = _extract_text(event.get("message", ""))
            message_type = event.get("message_type", "unknown")
            user_id = event.get("user_id", 0)
            group_id = event.get("group_id")
            sender = event.get("sender", {})

            log_raw = fw.config.get("log", {}).get("log_raw_message", True)
            if log_raw:
                log_broker.log_message(bot_name, message_type, user_id, group_id,
                                       raw_message, event.get("message_id"))
            else:
                source = f"群{group_id}" if group_id else f"私聊{user_id}"
                log_broker.log("message", "INFO",
                               f"[{bot_name}] {message_type} {source}: (原始内容未记录)",
                               {"bot": bot_name, "message_type": message_type,
                                "user_id": user_id, "group_id": group_id})

            fw.stats_writer.register_user(user_id, sender, message_type, group_id)
            await fw.router.route(event, bot_name)

        elif event_type == "notice":
            await handle_notice(fw, event, bot_name)
        elif event_type == "request":
            await handle_request(fw, event, bot_name)
    finally:
        try:
            await fw.hooks.trigger_async(
                HookPoints.EVENT_AFTER_DISPATCH, event, bot_name)
        except Exception as e:
            logger.error(f"event.after_dispatch 扩展点异常: {e}")


def register_raw_message_handler(fw, plugin_name: str, handler, priority: int = 50):
    """注册插件原始消息处理器（同插件同 handler 去重，按优先级升序）"""
    for item in fw._raw_message_handlers:
        if item["plugin_name"] == plugin_name and item["handler"] == handler:
            return
    fw._raw_message_handlers.append({
        "plugin_name": plugin_name,
        "priority": priority,
        "handler": handler,
    })
    fw._raw_message_handlers.sort(key=lambda x: x["priority"])


def unregister_raw_message_handlers(fw, plugin_name: str):
    """移除某插件的全部原始消息处理器（插件卸载时调用）"""
    fw._raw_message_handlers = [
        item for item in fw._raw_message_handlers
        if item["plugin_name"] != plugin_name
    ]


async def dispatch_raw_message_handlers(fw, data: dict, bot_name: str) -> bool:
    """按插件优先级分发原始消息事件（任一返回 True 表示已接管）。"""
    if not fw._raw_message_handlers:
        return False
    for item in list(fw._raw_message_handlers):
        handler = item["handler"]
        try:
            if asyncio.iscoroutinefunction(handler):
                result = await handler(data, bot_name)
            else:
                result = await asyncio.to_thread(handler, data, bot_name)
            if result is True:
                log_broker.log_plugin(item["plugin_name"], "原始消息接管", {
                    "bot": bot_name,
                    "message_type": data.get("message_type", ""),
                    "user_id": data.get("user_id", 0),
                    "group_id": data.get("group_id"),
                })
                return True
        except Exception as e:
            logger.error(f"[{item['plugin_name']}] 原始消息处理器异常: {e}", exc_info=True)
    return False


async def handle_notice(fw, data: dict, bot_name: str = "default"):
    """处理通知事件"""
    notice_type = data.get("notice_type", "")
    await fw.event_bus.aemit(f"notice.{notice_type}", data)
    if notice_type == "group_increase":
        await asyncio.to_thread(sync_group_member_join, fw, data)
    elif notice_type == "group_decrease":
        await asyncio.to_thread(sync_group_member_leave, fw, data)


async def handle_request(fw, data: dict, bot_name: str = "default"):
    """处理请求事件"""
    request_type = data.get("request_type", "")
    await fw.event_bus.aemit(f"request.{request_type}", data)


def sync_group_member_join(fw, data: dict):
    """同步群成员加入"""
    try:
        fw.db.execute(
            "INSERT IGNORE INTO group_members (group_id, user_id) VALUES (%s, %s)",
            (data.get("group_id"), data.get("user_id")))
    except Exception as e:
        logger.error(f"同步群成员加入失败: {e}")


def sync_group_member_leave(fw, data: dict):
    """同步群成员离开"""
    try:
        fw.db.execute(
            "DELETE FROM group_members WHERE group_id = %s AND user_id = %s",
            (data.get("group_id"), data.get("user_id")))
    except Exception as e:
        logger.error(f"同步群成员离开失败: {e}")
