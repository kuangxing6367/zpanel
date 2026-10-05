# -*- coding: utf-8 -*-
"""Webhook 出站（内核机制）

框架主动往外推事件的能力：插件/系统注册「当事件 E 发生时，POST 到 URL」。
与事件总线区别：事件总线是进程内插件间通信；webhook 是跨进程/跨机出站通知。

设计：
- WebhookDispatcher 持有订阅（url + 事件白名单 + 可选 secret）；
- 注册订阅时为每个事件在 framework.event_bus 上 subscribe 一个 handler；
- 事件触发时，handler 异步把 payload POST 到目标 URL（urllib + 线程池，不阻塞事件循环）；
- secret 以 X-Hub-Signature（HMAC-SHA256）头校验，供接收方验真。
零第三方依赖。
"""
import hashlib
import hmac
import json
import logging
import threading
import urllib.request

logger = logging.getLogger('zernus')


class WebhookSubscription:
    def __init__(self, sub_id, url, events, secret=None, active=True):
        self.id = sub_id
        self.url = url
        self.events = list(events)
        self.secret = secret
        self.active = active


class WebhookDispatcher:
    def __init__(self, framework):
        self.framework = framework
        self._subs = {}            # sub_id -> WebhookSubscription
        self._next_id = 1
        self._lock = threading.Lock()
        self._executor = None      # 懒加载线程池

    # ---- 订阅管理 ----
    def add(self, url, events, secret=None) -> str:
        with self._lock:
            sub_id = f"wh_{self._next_id}"
            self._next_id += 1
            sub = WebhookSubscription(sub_id, url, events, secret)
            self._subs[sub_id] = sub
        self._bind(sub)
        return sub_id

    def remove(self, sub_id):
        with self._lock:
            sub = self._subs.pop(sub_id, None)
        if sub is None:
            return
        bus = getattr(self.framework, 'event_bus', None)
        if bus is not None:
            for ev in sub.events:
                try:
                    bus.unsubscribe(ev, f"webhook:{sub_id}", None)
                except Exception:
                    pass

    def list_subs(self):
        with self._lock:
            return [
                {'id': s.id, 'url': s.url, 'events': s.events,
                 'active': s.active}
                for s in self._subs.values()
            ]

    def _bind(self, sub):
        """为每个事件在事件总线订阅一个 handler，触发即出站。"""
        bus = getattr(self.framework, 'event_bus', None)
        if bus is None:
            return
        for ev in sub.events:
            bus.subscribe(ev, f"webhook:{sub.id}", self._make_handler(sub, ev))

    def _make_handler(self, sub, event_name):
        def handler(payload):
            self._post(sub, event_name, payload)
            return None
        return handler

    # ---- 出站投递 ----
    def _executor_get(self):
        if self._executor is None:
            from concurrent.futures import ThreadPoolExecutor
            self._executor = ThreadPoolExecutor(
                max_workers=4, thread_name_prefix='webhook')
        return self._executor

    def _post(self, sub, event_name, payload):
        try:
            body = json.dumps({
                'event': event_name,
                'payload': payload,
            }, ensure_ascii=False, default=str).encode('utf-8')
            req = urllib.request.Request(sub.url, data=body, method='POST')
            req.add_header('Content-Type', 'application/json')
            req.add_header('X-Zeronus-Event', event_name)
            if sub.secret:
                sig = hmac.new(
                    sub.secret.encode('utf-8'), body, hashlib.sha256
                ).hexdigest()
                req.add_header('X-Hub-Signature', f"sha256={sig}")
            self._executor_get().submit(self._do_post, req, sub)
        except Exception as e:
            logger.error(f"Webhook 投递准备失败 [{sub.url}]: {e}")

    @staticmethod
    def _do_post(req, sub):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status >= 400:
                    logger.warning(f"Webhook 目标返回 {resp.status}: {sub.url}")
        except Exception as e:
            logger.warning(f"Webhook 投递失败 [{sub.url}]: {e}")
