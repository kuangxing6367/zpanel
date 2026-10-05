"""
多轮会话（core/ctx 节点 12）

wait_for / create_session 委托给 services['session_manager']（由 extensions.session 提供）。
"""


class SessionMixin:
    """ctx 上的多轮会话能力（依赖 session_manager 服务）"""

    async def wait_for(self, event, prompt=None, timeout=60, handler=None):
        """
        等待用户下一条消息（多轮会话）
        :return: 消息 dict 或 None（超时）
        """
        mgr = self._framework.services.get('session_manager')
        if mgr is None:
            raise RuntimeError("会话管理器未加载（请启用 extensions.session）")
        return await mgr.wait_for(self, event, prompt, timeout, handler)

    def create_session(self, event, timeout=60):
        """
        创建会话对象（支持 async with）
        用法：
            async with ctx.create_session(event, timeout=120) as sess:
                name = await sess.ask("你叫什么名字？")
        """
        mgr = self._framework.services.get('session_manager')
        if mgr is None:
            raise RuntimeError("会话管理器未加载（请启用 extensions.session）")
        return mgr.session_context(self, event, timeout)
