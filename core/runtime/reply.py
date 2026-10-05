"""协议中立的回复 / 生命周期钩子节点。"""
from __future__ import annotations

import logging

from core.hooks import HookPoints
from core.adapters.protocol import ProtocolAdapter

logger = logging.getLogger("zernus")


def on_message_sent(fw, bot_name: str, action: str, params: dict, resp: dict):
    """消息发送成功后的生命周期钩子（转发为 after_message_sent 事件）"""
    try:
        task = fw.loop.create_task(fw.event_bus.aemit("after_message_sent", {
            "bot": bot_name,
            "action": action,
            "params": params,
            "response": resp,
        }))
        fw._pending_tasks.add(task)
        task.add_done_callback(fw._pending_tasks.discard)
    except Exception as e:
        logger.debug(f"after_message_sent 事件派发失败: {e}")


async def reply_text(fw, target, text: str):
    """协议中立的「框架自动回复一条文本」统一入口。"""
    def _g(key):
        if isinstance(target, dict):
            return target.get(key)
        return getattr(target, key, None)

    group_id = _g("group_id")
    user_id = _g("user_id")
    is_group = _g("is_group")
    if is_group is None:
        is_group = bool(group_id)
    source = _g("bot_name")

    payload = {"text": text, "group_id": group_id, "user_id": user_id, "source": source}
    try:
        if False in (await fw.hooks.trigger_async(HookPoints.MESSAGE_BEFORE_SEND, payload)):
            logger.debug("消息发送被扩展点 message.before_send 取消")
            return None
    except Exception as e:
        logger.error(f"message.before_send 扩展点异常: {e}")

    adapter = fw.services.get("protocol_adapter")
    result = None
    if adapter is not None and type(adapter).send_text is not ProtocolAdapter.send_text:
        try:
            result = await adapter.send_text(
                text,
                group_id=group_id if is_group else None,
                user_id=None if is_group else user_id,
                source=source,
            )
        except Exception as e:
            logger.warning(f"接入端 send_text 失败，回退通用调用: {e}")

    if result is None:
        caller = fw.services.get("api_caller")
        if caller is None:
            logger.debug("无可用接入端，框架自动回复被跳过")
            return None
        tgt = {"group_id": group_id} if is_group else {"user_id": user_id}
        result = await caller.acall("send_msg", **tgt, message=text)

    try:
        await fw.hooks.trigger_async(
            HookPoints.MESSAGE_AFTER_SEND, {**payload, "result": result})
    except Exception as e:
        logger.error(f"message.after_send 扩展点异常: {e}")
    return result
