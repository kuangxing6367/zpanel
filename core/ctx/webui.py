"""
WebUI 内嵌与注册期集合访问（core/ctx 节点 11）

- dashboard_card / webui / override_webui / register_*_extension：注册 WebUI 相关扩展
- _get_* 访问器：框架在注册周期结束时拉取本次收集的命令/任务/卡片/扩展/处理器
"""

import logging

logger = logging.getLogger('zernus')


class WebUiMixin:
    """ctx 上的 WebUI 注册与注册期集合访问器"""

    def dashboard_card(self, title: str, handler, icon: str = None, priority: int = 50):
        """
        注册一个仪表盘卡片
        handler 返回 dict: {title, value, label, icon, color}
        """
        self._dashboard_cards.append({
            'plugin_name': self._plugin_name,
            'title': title,
            'handler': handler,
            'icon': icon,
            'priority': priority,
        })

    def webui(self, title: str, entry: str = 'index.html', icon: str = None, order: int = 50,
              sidebar: bool = False):
        """
        注册插件 WebUI 页面
        插件目录下的 web/ 子目录中的 HTML/JS/CSS 文件将被框架内嵌展示
        """
        self._framework.plugin_loader.register_webui(self._plugin_name, {
            'title': title,
            'entry': entry,
            'icon': icon,
            'order': order,
            'sidebar': sidebar,
        })

    def override_webui(self):
        """让本插件接管整个 Web 前端（框架根路由与静态资源改由本插件 web/ 目录服务）"""
        return self._framework.plugin_loader.override_webui(self._plugin_name)

    def register_group_extension(self, key: str, title: str, handler,
                                ext_type: str = 'column'):
        """
        注册 WebUI「群组管理」页插件扩展
        handler(group_id) -> 字符串（column）或 {label:value} 字典（panel）
        """
        if not callable(handler):
            raise TypeError(f"handler '{getattr(handler, '__name__', handler)}' 不可调用")
        self._group_extensions.append({
            'key': key, 'title': title, 'handler': handler,
            'type': ext_type if ext_type in ('column', 'panel') else 'column',
        })

    def register_user_extension(self, key: str, title: str, handler,
                                ext_type: str = 'column'):
        """
        注册 WebUI「用户管理」页插件扩展（参数同 register_group_extension，
        handler 签名：handler(user_id) -> 字符串 或 {label: value} 字典）
        """
        if not callable(handler):
            raise TypeError(f"handler '{getattr(handler, '__name__', handler)}' 不可调用")
        self._user_extensions.append({
            'key': key, 'title': title, 'handler': handler,
            'type': ext_type if ext_type in ('column', 'panel') else 'column',
        })

    # ---- 注册期集合访问器（供框架 loader 拉取）----

    def _get_group_extensions(self) -> list:
        """获取本次注册周期收集的群组页扩展"""
        return list(self._group_extensions)

    def _get_user_extensions(self) -> list:
        """获取本次注册周期收集的用户页扩展"""
        return list(self._user_extensions)

    def _get_raw_message_handlers(self) -> list:
        """获取本次注册周期收集的原始消息处理器"""
        return list(self._raw_message_handlers)

    def _get_commands(self) -> list:
        return list(self._commands)

    def _get_tasks(self) -> list:
        return list(self._tasks)

    def _get_dashboard_cards(self) -> list:
        return list(self._dashboard_cards)
