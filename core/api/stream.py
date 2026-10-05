# -*- coding: utf-8 -*-
"""
内核实时推送的**订阅核心**（与传输方式无关）

只做「topic → 多个订阅者」的广播，不含任何 HTTP/WebSocket 细节 ——
传输层（`core/api/ws.py`）负责把队列里的东西送给客户端。

## 票据（ticket）

浏览器 `WebSocket` 和 `EventSource` 一样**不能设置请求头**，登录 token 没法走
`Authorization`。直接把 token 拼在 URL 上会让它进访问日志 / 代理日志 / 浏览器历史，
运维面板不能这么干。

做法：先用一个带鉴权的普通接口换一张**短时票据**，再用票据去连推送通道。
票据由 HMAC 签名、绑定具体 topic、**一次性**、默认 60 秒过期。
于是 URL 上出现的永远是一个「60 秒内、只能用一次、只能订阅这一个 topic」的串，
不是长期有效的登录凭据。

## 背压

每个订阅者一个有界队列，满了丢最旧的 —— **绝不阻塞发布方**。
实例输出如果被慢客户端拖住，等于把面板变成实例的瓶颈，这不能接受
（和帧流面 `StreamMux` 的取舍一致）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import queue
import secrets
import threading
import time

DEFAULT_TTL = 60          # 票据有效秒数
MAX_QUEUE = 512           # 单个订阅者的缓冲条数


class StreamHub:
    """topic → 多个订阅者的广播器（进程内）。"""

    def __init__(self, ttl: int = DEFAULT_TTL, max_queue: int = MAX_QUEUE, log=None):
        self.ttl = int(ttl)
        self.max_queue = int(max_queue)
        self._log = log or (lambda m: None)
        self._secret = secrets.token_bytes(32)     # 进程内随机：重启即全部票据失效
        self._subs = {}                            # topic -> set(Queue)
        self._used = set()                         # 已用过的票据签名
        self._lock = threading.Lock()
        self._published = 0
        self._dropped = 0

    # ── 票据 ────────────────────────────────────────────────
    def _sign(self, topic: str, exp: int, nonce: str) -> str:
        return hmac.new(self._secret, f"{topic}|{exp}|{nonce}".encode(),
                        hashlib.sha256).hexdigest()[:32]

    def new_ticket(self, topic: str, ttl: int = 0) -> dict:
        exp = int(time.time()) + int(ttl or self.ttl)
        # 必须带随机 nonce：如果签名只由 topic+exp 决定，同一秒内给同一 topic
        # 发两张票会得到**相同的票**，第二张被误判成"已使用"（本机踩过）。
        nonce = secrets.token_hex(8)
        payload = f"{exp}|{nonce}|{self._sign(topic, exp, nonce)}"
        return {'ticket': base64.urlsafe_b64encode(payload.encode()).decode().rstrip('='),
                'topic': topic, 'exp': exp, 'ttl': int(ttl or self.ttl)}

    def check_ticket(self, topic: str, ticket: str) -> tuple:
        """校验票据，返回 (ok, reason)。通过即作废该票（一次性）。"""
        if not ticket:
            return False, '缺少票据'
        try:
            payload = base64.urlsafe_b64decode(ticket + '=' * (-len(ticket) % 4)).decode()
            exp_s, nonce, sig = payload.split('|', 2)
            exp = int(exp_s)
        except Exception:
            return False, '票据格式非法'
        if exp < time.time():
            return False, '票据已过期（请重新申请）'
        if not hmac.compare_digest(sig, self._sign(topic, exp, nonce)):
            return False, '票据签名不匹配（topic 不对或已被篡改）'
        with self._lock:
            if sig in self._used:
                return False, '票据已使用过（一次性）'
            self._used.add(sig)
            if len(self._used) > 4096:
                self._used.clear()
        return True, ''

    # ── 订阅 ────────────────────────────────────────────────
    def subscribe(self, topic: str) -> queue.Queue:
        q = queue.Queue(maxsize=self.max_queue)
        with self._lock:
            self._subs.setdefault(topic, set()).add(q)
        return q

    def unsubscribe(self, topic: str, q):
        with self._lock:
            s = self._subs.get(topic)
            if s:
                s.discard(q)
                if not s:
                    self._subs.pop(topic, None)

    def publish(self, topic: str, event: str, data) -> int:
        """广播，返回送达条数（0 = 无人订阅）。"""
        with self._lock:
            subs = list(self._subs.get(topic) or ())
        if not subs:
            return 0
        item = (event, data, time.time())
        n = 0
        for q in subs:
            try:
                q.put_nowait(item); n += 1
            except queue.Full:
                try:
                    q.get_nowait(); q.put_nowait(item); n += 1
                    self._dropped += 1
                except Exception:
                    pass
        self._published += 1
        return n

    def stats(self) -> dict:
        with self._lock:
            topics = {t: len(s) for t, s in self._subs.items()}
        return {'topics': topics, 'subscribers': sum(topics.values()),
                'published': self._published, 'dropped': self._dropped}
