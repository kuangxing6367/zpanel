"""
消息与群组动作快捷方法（core/ctx 节点 7）

sync/async 成对：send_msg/asend_msg、ban/aban、kick/akick、mute_all/amute_all、
set_card/aset_card、get_member_list/aget_member_list、get_member_info/aget_member_info。
底层统一走 `self.onebot`（协议无关封装，由接入端注册）。
"""


class MessagingMixin:
    """ctx 上的 OneBot/协议动作快捷方法"""

    def send_msg(self, user_id: int = None, group_id: int = None,
                 message=None, auto_escape: bool = False, bot: str = None):
        """快捷发送消息，支持通过 user_id/group_id 自动判断私聊/群聊（同步桥接）"""
        if bot is None:
            bot = getattr(self, '_current_bot', None)
        return self.onebot.send_msg(
            user_id=user_id, group_id=group_id,
            message=message, auto_escape=auto_escape, bot=bot
        )

    async def asend_msg(self, user_id: int = None, group_id: int = None,
                        message=None, auto_escape: bool = False, bot: str = None):
        """异步快捷发送消息（推荐 async handler 使用，不阻塞事件循环）"""
        if bot is None:
            bot = getattr(self, '_current_bot', None)
        return await self.onebot.acall(
            'send_msg', user_id=user_id, group_id=group_id,
            message=message, auto_escape=auto_escape, bot=bot
        )

    def ban(self, group_id: int, user_id: int, duration: int = 600, bot: str = None):
        """快捷禁言群成员（duration=0 解禁）（同步桥接）"""
        return self.onebot.set_group_ban(group_id, user_id, duration=duration, bot=bot)

    async def aban(self, group_id: int, user_id: int, duration: int = 600, bot: str = None):
        """异步快捷禁言群成员"""
        return await self.onebot.acall(
            'set_group_ban', group_id=group_id, user_id=user_id,
            duration=duration, bot=bot
        )

    def kick(self, group_id: int, user_id: int, reject_add_request: bool = False, bot: str = None):
        """快捷踢出群成员（同步桥接）"""
        return self.onebot.set_group_kick(group_id, user_id, reject_add_request=reject_add_request, bot=bot)

    async def akick(self, group_id: int, user_id: int,
                    reject_add_request: bool = False, bot: str = None):
        """异步快捷踢出群成员"""
        return await self.onebot.acall(
            'set_group_kick', group_id=group_id, user_id=user_id,
            reject_add_request=reject_add_request, bot=bot
        )

    def mute_all(self, group_id: int, enable: bool = True, bot: str = None):
        """快捷全员禁言/解禁（同步桥接）"""
        return self.onebot.set_group_whole_ban(group_id, enable=enable, bot=bot)

    async def amute_all(self, group_id: int, enable: bool = True, bot: str = None):
        """异步快捷全员禁言/解禁"""
        return await self.onebot.acall(
            'set_group_whole_ban', group_id=group_id, enable=enable, bot=bot
        )

    def set_card(self, group_id: int, user_id: int, card: str, bot: str = None):
        """快捷设置群名片（空字符串清除名片）（同步桥接）"""
        return self.onebot.set_group_card(group_id, user_id, card=card, bot=bot)

    async def aset_card(self, group_id: int, user_id: int, card: str, bot: str = None):
        """异步快捷设置群名片"""
        return await self.onebot.acall(
            'set_group_card', group_id=group_id, user_id=user_id, card=card, bot=bot
        )

    def get_member_list(self, group_id: int, bot: str = None):
        """快捷获取群成员列表（同步桥接）"""
        return self.onebot.get_group_member_list(group_id=group_id, bot=bot)

    async def aget_member_list(self, group_id: int, bot: str = None):
        """异步快捷获取群成员列表"""
        return await self.onebot.acall('get_group_member_list', group_id=group_id, bot=bot)

    def get_member_info(self, group_id: int, user_id: int, bot: str = None):
        """快捷获取群成员信息（同步桥接）"""
        return self.onebot.get_group_member_info(group_id, user_id, bot=bot)

    async def aget_member_info(self, group_id: int, user_id: int, bot: str = None):
        """异步快捷获取群成员信息"""
        return await self.onebot.acall(
            'get_group_member_info', group_id=group_id, user_id=user_id, bot=bot
        )
