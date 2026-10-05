"""
命令/任务/事件注册（core/ctx 节点 3）

按用户既定原则：命令注册本质是「注册 (name→程序)」的薄分发原语，
pattern/优先级/别名/权限门控这些是下游策略，本 mixin 仅负责把调用收集进注册集合，
由框架/适配器层决定如何路由。事件订阅/发布桥接到框架内核的事件总线。
"""

import logging

logger = logging.getLogger('zernus')


class RegistrationMixin:
    """命令、定时任务、事件订阅与发布的注册入口"""

    # ---- 命令注册 ----

    def command(self, pattern: str, handler, priority: int = 50,
                dynamic: bool = False, alias: str = None, description: str = None,
                require_admin: bool = False, require_superuser: bool = False,
                require_perm: str = None):
        """
        注册一个命令
        :param pattern: 正则表达式或命令名（主匹配模式）
        :param handler: 处理函数 (event, match) -> None
        :param priority: 优先级，越小越优先
        :param dynamic: 是否为动态命令（dynamic=True 表示该命令在动态命令 tab 展示，仅标记用）
        :param alias: 命令别名，逗号分隔的字符串或列表（如 "/help,/h" 或 ["/help", "/h"]）
        :param description: 命令描述文本
        :param require_admin: 需要管理员/群主/超管权限（旧的身份轴判定）
        :param require_superuser: 需要超级管理员权限（高于 require_admin）
        :param require_perm: 需要的权限节点（新的权限组判定），如 'myplugin.ban'
        """
        if require_superuser:
            require_admin = False  # super 优先级更高

        if not callable(handler):
            raise TypeError(f"handler '{handler.__name__ if hasattr(handler, '__name__') else handler}' 不可调用")

        # 规范化 alias 为字符串
        if alias is not None:
            if isinstance(alias, (list, tuple)):
                alias = ','.join(str(a).strip() for a in alias)
            else:
                alias = str(alias).strip()

        # 从 handler 的 docstring 自动提取描述（如果未显式传入）
        if description is None and handler.__doc__:
            description = handler.__doc__.strip().split('\n')[0].strip()

        # 所有命令统一收集到列表中，由框架批量写入 commands 表
        self._commands.append({
            'plugin_name': self._plugin_name,
            'pattern': pattern,
            'alias': alias,
            'description': description,
            'priority': priority,
            'handler': handler,
            'handler_name': handler.__name__,
            'is_dynamic': 1 if dynamic else 0,
            'require_level': 'super' if require_superuser else ('admin' if require_admin else ''),
            'require_perm': (require_perm or '').strip().lower(),
        })

    # ---- 定时任务 ----

    def task(self, cron_expr: str, executor, description: str = None):
        """注册定时任务"""
        if not callable(executor):
            raise TypeError(f"executor '{executor.__name__ if hasattr(executor, '__name__') else executor}' 不可调用")

        self._tasks.append({
            'plugin_name': self._plugin_name,
            'cron_expression': cron_expr,
            'handler': executor,
            'handler_name': executor.__name__,
            'description': description or f"{self._plugin_name} 定时任务",
        })

    # ---- 事件订阅/发布 ----

    def on(self, event_name: str, handler):
        """订阅系统事件（handler 支持 async def 和普通 def）"""
        self._framework.event_bus.subscribe(event_name, self._plugin_name, handler)

    def on_raw_message(self, handler):
        """
        注册原始消息处理器（原始消息注入点）

        该处理器收到的是**原始消息事件**（完整 dict，含全部消息段，未提取纯文本），
        在框架命令匹配/关键词回复之前触发，供选择性使用：
        - handler 返回 True        → 消息被接管，框架跳过对该消息的后续全部处理
        - handler 返回 None/False  → 消息继续走正常流程（命令匹配/关键词兜底等）

        签名：handler(raw_event: dict, bot_name: str) -> bool | None
        """
        if not callable(handler):
            raise TypeError(f"handler '{getattr(handler, '__name__', handler)}' 不可调用")
        self._raw_message_handlers.append(handler)

    def emit(self, event_name: str, payload: dict = None):
        """发布事件（同步桥接，供旧插件使用）"""
        self._framework.event_bus.emit(event_name, payload)

    async def aemit(self, event_name: str, payload: dict = None):
        """异步发布事件（推荐 async handler 使用，不阻塞事件循环）"""
        await self._framework.event_bus.aemit(event_name, payload)

    # ---- 扩展点（内核契约：把行为挂到内核的任意运行环节）----

    def hook(self, point: str, handler, priority: int = 50):
        """
        在内核的**扩展点**注册一个处理器——这是"往框架里插入自己的逻辑"的统一入口，
        可在几乎任意运行环节挂接行为：

          - 'lifecycle.startup' / 'lifecycle.shutdown'   进程启动 / 关闭
          - 'http.before_request' / 'http.after_request' Web 请求前后（before 可返回 Response 短路）
          - 'event.before_dispatch' / 'event.after_dispatch'  事件进入内核前后（before 返回 False 丢弃）
          - 'command.before' / 'command.after'          命令执行前后（before 返回 False 跳过）
          - 'message.before_send' / 'message.after_send' 框架主动发文本前后
          - 'action.before' / 'action.after'            任意协议动作调用前后（通知，不短路）

        handler 可以是普通函数或 `async def`；同名（本插件内）重复注册自动去重。
        也可注册自定义扩展点（任意字符串点位），由你自己的代码触发。
        """
        if not callable(handler):
            raise TypeError(f"hook 的 handler 必须可调用: {getattr(handler, '__name__', handler)}")
        name = f"{self._plugin_name}:{point}"
        self._framework.hooks.register(point, name, handler, priority)
        return True

    def unhook(self, point: str):
        """
        注销本插件在该扩展点的全部处理器（插件卸载/重载前清理用）。
        """
        name = f"{self._plugin_name}:{point}"
        self._framework.hooks.unregister(point, name)
        return True

    def get_text(self, message_or_event) -> str:
        """
        从消息中提取纯文本。入参可以是：
          - OneBot 消息（str，或富媒体段列表）
          - 事件对象 / dict（自动取 message / raw_message / text 字段）
        富媒体（图片等）被剥离，只保留文本内容。
        """
        from core.messaging.event import _extract_text
        if isinstance(message_or_event, dict):
            text = (message_or_event.get('message')
                    or message_or_event.get('raw_message')
                    or message_or_event.get('text'))
        elif hasattr(message_or_event, 'message'):
            text = getattr(message_or_event, 'message', None) \
                or getattr(message_or_event, 'raw_message', None)
        else:
            text = message_or_event
        return _extract_text(text or '')
