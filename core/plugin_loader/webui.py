"""
WebUI / 仪表盘 / 前端接管 mixin

管理插件上报的仪表盘卡片、群组/用户管理页扩展、以及「接管整个前端」能力。
卡片与扩展 handler 在共享线程池中执行并限时 2 秒，避免慢操作卡死 Web 线程。
"""
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

logger = logging.getLogger('zernus')


# 仪表盘卡片执行线程池（共享，避免每次请求创建线程；慢卡片隔离在此池）
_cards_executor = None


class WebUiMixin:
    """仪表盘卡片、WebUI 扩展与前端接管。"""

    def _sync_dashboard_cards(self, plugin_name: str, cards: list):
        """存储仪表盘卡片信息到插件信息中"""
        with self._lock:
            info = self._loaded_plugins.get(plugin_name)
            if info:
                info['dashboard_cards'] = cards

    def get_dashboard_cards(self) -> list:
        """
        获取所有仪表盘卡片（按优先级排序）

        每个卡片 handler 在独立线程池中执行并限时 2 秒：防止某个插件卡片的慢操作
        （如内存统计、网络请求）占满 Web 线程池导致仪表盘卡死；超时的卡片
        返回占位数据并记录告警（超时任务在后台继续跑，不阻塞 Web 线程）。
        """
        global _cards_executor
        result = []
        with self._lock:
            items = []
            for name, info in self._loaded_plugins.items():
                cards = info.get('dashboard_cards', [])
                for card in cards:
                    items.append((name, card))
        if not items:
            return result

        if _cards_executor is None:
            _cards_executor = ThreadPoolExecutor(
                max_workers=4, thread_name_prefix='zccards')
        futures = {}
        for name, card in items:
            handler = card['handler']
            fut = _cards_executor.submit(handler)
            futures[fut] = (name, card)
        for fut, (name, card) in futures.items():
            try:
                card_data = fut.result(timeout=2.0)
            except FutureTimeout:
                logger.warning(f"[{name}] 仪表盘卡片执行超时（>2s），已跳过: {card.get('title', '')}")
                card_data = {"value": "⏳ 加载超时", "label": card.get('title', ''), "timeout": True}
                fut.cancel()
            except Exception as e:
                logger.error(f"[{name}] 仪表盘卡片异常: {e}")
                card_data = None
            if card_data:
                result.append({
                    'plugin_name': name,
                    'title': card.get('title', ''),
                    'icon': card.get('icon'),
                    'priority': card.get('priority', 50),
                    'data': card_data,
                })
        result.sort(key=lambda x: x['priority'])
        return result

    def get_ui_extensions(self, scope: str) -> list:
        """
        获取群组(scope='groups')或用户(scope='users')管理页的全部插件扩展元信息
        返回: [{key, title, plugin, type}]
        """
        field = 'group_extensions' if scope == 'groups' else 'user_extensions'
        result = []
        with self._lock:
            for name, info in self._loaded_plugins.items():
                for ext in info.get(field, []):
                    result.append({
                        'key': ext['key'],
                        'title': ext['title'],
                        'plugin': name,
                        'type': ext['type'],
                    })
        result.sort(key=lambda x: (x['plugin'], x['title']))
        return result

    def _get_ext_handler(self, scope: str, key: str):
        """按 scope+key 找到扩展 handler（未找到返回 None）"""
        field = 'group_extensions' if scope == 'groups' else 'user_extensions'
        with self._lock:
            for info in self._loaded_plugins.values():
                for ext in info.get(field, []):
                    if ext['key'] == key:
                        return ext['handler']
        return None

    def call_ui_extensions(self, scope: str, target_id, keys=None) -> dict:
        """
        对单个群/用户调用扩展 handler（线程池 + 2 秒超时隔离，不卡 Web 线程）

        :param scope: 'groups' | 'users'
        :param target_id: group_id 或 user_id
        :param keys: 要调用的扩展 key 列表（None=全部）
        :return: {key: {type, data, plugin}}；超时/异常返回占位
        """
        global _cards_executor
        field = 'group_extensions' if scope == 'groups' else 'user_extensions'
        exts = []
        with self._lock:
            for name, info in self._loaded_plugins.items():
                for ext in info.get(field, []):
                    if keys is None or ext['key'] in keys:
                        exts.append((name, ext))
        if not exts:
            return {}

        if _cards_executor is None:
            _cards_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='zccards')
        out = {}
        futures = {}
        for name, ext in exts:
            handler = ext['handler']
            fut = _cards_executor.submit(handler, target_id)
            futures[fut] = (name, ext)
        for fut, (name, ext) in futures.items():
            try:
                data = fut.result(timeout=2.0)
            except FutureTimeout:
                data = "⏳ 超时"
                fut.cancel()
            except Exception as e:
                logger.error(f"[{name}] UI 扩展[{ext['key']}] 异常: {e}")
                data = "-"
            out[ext['key']] = {
                'type': ext['type'],
                'plugin': name,
                'title': ext['title'],
                'data': data,
            }
        return out

    def register_webui(self, plugin_name: str, webui_info: dict):
        """注册插件的 WebUI 页面"""
        with self._lock:
            info = self._loaded_plugins.get(plugin_name)
            if info:
                # 避免重复注册
                webuis = info.setdefault('webuis', [])
                # 查找是否已存在同名 WebUI
                existing = next((w for w in webuis if w['title'] == webui_info['title']), None)
                if not existing:
                    webui_info['plugin_name'] = plugin_name
                    webuis.append(webui_info)

    def override_webui(self, plugin_name: str):
        """
        指定插件接管整个前端（根路由与静态资源从该插件 web/ 目录服务）。
        插件禁用/卸载时自动回退框架默认前端。
        """
        with self._lock:
            if plugin_name in self._loaded_plugins:
                self._override_webui = plugin_name
                return True
            return False

    def clear_override_webui(self, plugin_name: str = None):
        """清除前端接管。plugin_name 为 None 时清除全部；否则仅在匹配时清除。"""
        with self._lock:
            if plugin_name is None or self._override_webui == plugin_name:
                self._override_webui = None

    def get_override_webui(self) -> str:
        """获取当前接管前端的插件名（无则返回 None）"""
        with self._lock:
            return self._override_webui

    def get_override_webui_path(self) -> str:
        """获取接管前端的插件 web/ 目录（无接管或插件已卸载则返回 None）"""
        name = self.get_override_webui()
        if not name:
            return None
        path = self.get_plugin_webui_path(name)
        if not path:
            self.clear_override_webui(name)
            return None
        return path

    def get_plugin_webuis(self) -> list:
        """获取所有已注册的插件 WebUI 列表（按 order 排序）"""
        result = []
        with self._lock:
            for name, info in self._loaded_plugins.items():
                for w in info.get('webuis', []):
                    result.append({
                        'plugin_name': name,
                        'title': w.get('title', ''),
                        'entry': w.get('entry', 'index.html'),
                        'icon': w.get('icon'),
                        'order': w.get('order', 50),
                        'sidebar': bool(w.get('sidebar', False)),
                    })
        result.sort(key=lambda x: x['order'])
        return result

    def get_plugin_webui_path(self, plugin_name: str) -> str:
        """获取插件 web/ 目录的绝对路径"""
        plugin_path = os.path.join(self.plugins_dir, plugin_name, 'web')
        return plugin_path if os.path.isdir(plugin_path) else None

    def get_plugin_webui_entry(self, plugin_name: str, entry: str = 'index.html') -> str:
        """获取插件 WebUI 入口文件的完整路径"""
        web_dir = self.get_plugin_webui_path(plugin_name)
        if not web_dir:
            return None
        entry_path = os.path.join(web_dir, entry)
        return entry_path if os.path.isfile(entry_path) else None
