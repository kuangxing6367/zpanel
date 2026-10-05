"""异步统计批量写库器（内核细模块，依赖注入 db + 可选执行器）。

职责：把「用户/群自动注册」「命令命中计数」等高频写入聚合后批量落库，
由后台任务周期性 flush，不阻塞事件循环。db 为 duck-typed：只需提供
``execute(sql, params)``（框架的数据库层负责 MySQL/SQLite 翻译）。

与框架解耦：不引用任何 framework.* 模块。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger("zernus")


class AsyncStatsWriter:
    def __init__(self, db, executor=None, flush_interval: float = 5.0):
        self.db = db                      # 提供 .execute(sql, params)
        self._executor = executor        # 可选：独立线程池（与默认池隔离）
        self.flush_interval = flush_interval
        self._reg_queue = asyncio.Queue(maxsize=20000)
        self._dropped = 0
        self._cmd_hits: dict = {}
        self._kw_hits: dict = {}
        self._task = None

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="stats-writer")

    def command_hit(self, cmd_id: int):
        self._cmd_hits[cmd_id] = self._cmd_hits.get(cmd_id, 0) + 1

    def keyword_hit(self, kw_id: int):
        self._kw_hits[kw_id] = self._kw_hits.get(kw_id, 0) + 1

    def register_user(self, user_id: int, sender: dict, message_type: str, group_id: int = None):
        try:
            self._reg_queue.put_nowait((user_id, dict(sender), message_type, group_id))
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 1000 == 1:
                logger.warning(f"用户注册队列已满，已丢弃 {self._dropped} 条注册请求（消息量过大）")

    async def _run(self):
        while True:
            try:
                await asyncio.sleep(self.flush_interval)
                await self._run_in_db_thread(self._flush)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"统计批量写库异常: {e}")
                await asyncio.sleep(1)

    async def _run_in_db_thread(self, func):
        if self._executor is not None:
            return await asyncio.get_running_loop().run_in_executor(self._executor, func)
        return await asyncio.to_thread(func)

    def _flush(self):
        hits = self._cmd_hits
        self._cmd_hits = {}
        if hits:
            for cmd_id, cnt in hits.items():
                try:
                    self.db.execute(
                        "UPDATE commands SET hit_count = hit_count + %s WHERE id = %s",
                        (cnt, cmd_id))
                except Exception as e:
                    logger.error(f"命令命中计数写库失败 [{cmd_id}]: {e}")

        kwhits = self._kw_hits
        self._kw_hits = {}
        if kwhits:
            for kw_id, cnt in kwhits.items():
                try:
                    self.db.execute(
                        "UPDATE dynamic_commands SET hit_count = hit_count + %s WHERE id = %s",
                        (cnt, kw_id))
                except Exception as e:
                    logger.error(f"关键词命中计数写库失败 [{kw_id}]: {e}")

        items = []
        while True:
            try:
                items.append(self._reg_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        for item in items:
            try:
                self._register_one(*item)
            except Exception as e:
                logger.error(f"自动注册用户失败: {e}")

    def _register_one(self, user_id: int, sender: dict, message_type: str, group_id: int):
        if not user_id:
            return
        nickname = sender.get("nickname", "") or sender.get("card", "") or str(user_id)
        card = sender.get("card", "")

        self.db.execute(
            "INSERT INTO users (user_id, nickname, first_seen_at, last_active_at) "
            "VALUES (%s, %s, NOW(), NOW()) "
            "ON DUPLICATE KEY UPDATE "
            "nickname = IF(VALUES(nickname) != '', VALUES(nickname), nickname), "
            "last_active_at = NOW()",
            (user_id, nickname))

        if group_id and message_type == "group":
            group_name = sender.get("group_name", "")
            self.db.execute(
                "INSERT INTO groups_info (group_id, group_name, is_active, join_at) "
                "VALUES (%s, %s, 1, NOW()) "
                "ON DUPLICATE KEY UPDATE "
                "is_active = 1, "
                "group_name = IF(VALUES(group_name) != '', VALUES(group_name), group_name)",
                (group_id, group_name))

            role = sender.get("role", "member")
            title = sender.get("title", "")
            self.db.execute(
                "INSERT INTO group_members (group_id, user_id, card, role, title, last_active_at, message_count) "
                "VALUES (%s, %s, %s, %s, %s, NOW(), 1) "
                "ON DUPLICATE KEY UPDATE "
                "card = IF(VALUES(card) != '', VALUES(card), card), "
                "role = VALUES(role), "
                "title = IF(VALUES(title) != '', VALUES(title), title), "
                "last_active_at = NOW(), "
                "message_count = message_count + 1",
                (group_id, user_id, card, role, title))

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await asyncio.to_thread(self._flush)
