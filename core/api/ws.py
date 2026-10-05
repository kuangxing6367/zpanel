# -*- coding: utf-8 -*-
"""
内核实时推送的传输层：WebSocket（RFC 6455，**纯标准库**）

为什么自己写而不是装 `flask-sock` / `gevent-websocket`：内核只允许依赖 SQLite + Flask。
而 WebSocket 本身并不复杂 —— 握手是 `base64(sha1(key + GUID))`，帧是「2 字节头 + 可选
扩展长度 + 4 字节掩码」。自己实现还顺带拿到两个好处：协议行为完全可控（帧大小上限、
空闲超时、背压策略），以及**不引入额外的并发模型**（不拖进 gevent/eventlet）。

## 连接形态

    ws://<host>:<port>/ws/<topic>?ticket=<短时票据>

- `<topic>` 形如 `instance.<id>`、`node.<name>`；一个连接只订阅一个 topic。
- 票据见 `core/api/stream.py`：短时、一次性、绑定 topic —— 因为 WebSocket 和 SSE 一样
  **不能设置请求头**，登录 token 不能直接放 URL 上。

## 线程模型

每个连接两个线程：一个读（处理 ping / close）、一个写（把订阅队列推给客户端）。
面板场景下连接数是个位数到几十，这个开销可以接受；换来的是读阻塞不会卡住推送。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import socket
import struct
import threading
import time
from urllib.parse import parse_qs, unquote, urlparse

logger = logging.getLogger('zernus')

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

MAX_FRAME = 1 << 20          # 单帧上限 1MB
IDLE_TIMEOUT = 300           # 空闲超时（秒）：无推送也无客户端帧就断开
READ_TIMEOUT = 320


# ── 帧编解码 ────────────────────────────────────────────────
def build_frame(payload: bytes, opcode: int = OP_TEXT) -> bytes:
    """组装一帧（服务端发帧**不掩码**，这是 RFC 的规定）。"""
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + payload


def _read_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接已关闭")
        buf += chunk
    return buf


def read_frame(sock: socket.socket):
    """读一帧，返回 (opcode, payload)。客户端帧**必须**掩码，不掩码直接判非法。"""
    b0, b1 = _read_exact(sock, 2)
    opcode = b0 & 0x0F
    masked = b1 & 0x80
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack(">H", _read_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _read_exact(sock, 8))[0]
    if length > MAX_FRAME:
        raise ValueError(f"帧过大 {length}B（上限 {MAX_FRAME}B）")
    if not masked:
        raise ValueError("客户端帧未掩码，违反 RFC 6455")
    mask = _read_exact(sock, 4)
    data = bytearray(_read_exact(sock, length))
    for i in range(length):
        data[i] ^= mask[i % 4]
    return opcode, bytes(data)


# ── 服务器 ──────────────────────────────────────────────────
class WSStreamServer:
    """把 StreamHub 的队列通过 WebSocket 推给浏览器。"""

    def __init__(self, hub, host: str = "127.0.0.1", port: int = 8001,
                 check_ticket=None, allowed_origins=None, log=None):
        self.hub = hub
        self.host = host
        self.port = int(port)
        self.check_ticket = check_ticket
        self.allowed_origins = list(allowed_origins or [])
        self._log = log or (lambda m: logger.info(m))
        self._sock = None
        self._running = False
        self._thread = None
        self._conns = set()
        self._lock = threading.Lock()

    # ── 生命周期 ────────────────────────────────────────────
    def start(self):
        if self._running:
            return
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.host, self.port))
            self._sock.listen(32)
        except OSError as e:
            self._log(f"[ws] 监听 {self.host}:{self.port} 失败: {e}")
            self._sock = None
            return
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True, name="zp-ws")
        self._thread.start()
        self._log(f"[ws] 实时推送监听 ws://{self.host}:{self.port}")

    def stop(self):
        self._running = False
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        with self._lock:
            conns = list(self._conns)
        for c in conns:
            try:
                c.close()
            except Exception:
                pass

    # ── 主循环 ──────────────────────────────────────────────
    def _serve(self):
        while self._running:
            try:
                conn, addr = self._sock.accept()
            except OSError:
                break
            conn.settimeout(READ_TIMEOUT)
            with self._lock:
                self._conns.add(conn)
            threading.Thread(target=self._handle, args=(conn, addr),
                             daemon=True, name=f"zp-ws-{addr[1]}").start()

    # ── 单连接 ──────────────────────────────────────────────
    def _handle(self, conn, addr):
        try:
            topic = self._handshake(conn)
            if topic is None:
                return
            q = self.hub.subscribe(topic)
            self._log(f"[ws] {addr[0]} 订阅 {topic}")
            stop = threading.Event()

            def reader():
                """读客户端帧：只处理 ping / close，其余（含文本）忽略。"""
                try:
                    while not stop.is_set():
                        opcode, payload = read_frame(conn)
                        if opcode == OP_CLOSE:
                            stop.set()
                            return
                        if opcode == OP_PING:
                            with self._lock:
                                conn.sendall(build_frame(payload, OP_PONG))
                except Exception:
                    stop.set()

            def writer():
                try:
                    conn.sendall(build_frame(json.dumps(
                        {"event": "open", "data": {"topic": topic}},
                        ensure_ascii=False).encode()))
                    last = time.time()
                    while not stop.is_set():
                        try:
                            event, data, ts = q.get(timeout=1.0)
                        except Exception:
                            if time.time() - last > IDLE_TIMEOUT:
                                stop.set()
                                break
                            continue
                        last = time.time()
                        msg = json.dumps({"event": event, "data": data},
                                         ensure_ascii=False)
                        conn.sendall(build_frame(msg.encode()))
                except Exception:
                    stop.set()

            tr = threading.Thread(target=reader, daemon=True)
            tw = threading.Thread(target=writer, daemon=True)
            tr.start(); tw.start()
            tr.join(); tw.join()
        except Exception as e:
            logger.debug("[ws] 连接处理异常 %s: %s", addr, e)
        finally:
            try:
                self.hub.unsubscribe(getattr(self, "_last_topic", ""), None)
            except Exception:
                pass
            with self._lock:
                self._conns.discard(conn)
            try:
                conn.close()
            except Exception:
                pass

    # ── 握手 ────────────────────────────────────────────────
    def _handshake(self, conn) -> str:
        """完成 RFC6455 握手并校验票据；成功返回 topic，失败返回 None（连接已关闭）。"""
        raw = b""
        while b"\r\n\r\n" not in raw and len(raw) < 8192:
            chunk = conn.recv(4096)
            if not chunk:
                return None
            raw += chunk
        try:
            head = raw.decode("utf-8", "replace")
            first, _, headers_blob = head.partition("\r\n")
            method, path, _ = first.split(" ", 2)
        except Exception:
            self._deny(conn, 400, "请求头解析失败")
            return None
        if method.upper() != "GET":
            self._deny(conn, 405, "只接受 GET")
            return None

        headers = {}
        for line in headers_blob.split("\r\n"):
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()

        if headers.get("upgrade", "").lower() != "websocket":
            self._deny(conn, 400, "缺少 Upgrade: websocket")
            return None

        # Origin 校验：有白名单就严格比对，没有则放行（同源部署场景）
        origin = headers.get("origin", "")
        if self.allowed_origins and origin and origin not in self.allowed_origins:
            self._deny(conn, 403, "Origin 不在白名单")
            return None

        u = urlparse(path)
        if not u.path.startswith("/ws/"):
            self._deny(conn, 404, "路径必须是 /ws/<topic>")
            return None
        topic = unquote(u.path[4:])

        ticket = parse_qs(u.query).get("ticket", [""])[0]
        if self.check_ticket:
            ok, why = self.check_ticket(topic, ticket)
            if not ok:
                self._deny(conn, 401, why)
                return None

        key = headers.get("sec-websocket-key", "")
        if not key:
            self._deny(conn, 400, "缺少 Sec-WebSocket-Key")
            return None
        accept = base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()
        resp = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        )
        conn.sendall(resp.encode())
        self._last_topic = topic      # 供 finally 清理（单连接单 topic，够用）
        return topic

    @staticmethod
    def _deny(conn, code: int, msg: str):
        try:
            body = msg.encode("utf-8")
            conn.sendall(
                f"HTTP/1.1 {code} {msg}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        except Exception:
            pass
