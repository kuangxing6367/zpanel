# -*- coding: utf-8 -*-
"""
节点控制通道 · hub 侧（内核级多机管理 · 控制面）

星型拓扑中心：监听一个 TCP 端口，等待节点**主动外连**（节点可在 NAT / 防火墙后，
hub 永不反向连接节点）。传输复用服务层 `service/transport/framed.py` 的编解码原语，
鉴权用 per-node 预共享密钥——HELLO 帧携带节点名，hub 按名从 `NodeRegistry` 取密钥
做整帧 HMAC 验签，伪造节点名无法通过。

帧类型（core 级统一语义）：
    1 HELLO      node → hub   握手注册，载荷 JSON {"name","version","platform"}
    2 HEARTBEAT  node → hub   心跳 / 状态上报，载荷 JSON（health 结构）
    3 CMD        hub  → node  命令下发，载荷 JSON {"id","cmd","args"}
    4 RESULT     node → hub   命令回执，载荷 JSON {"id","ok","data"}

与扩展版（software/extensions/node_control）的区别：
- 密钥来源是数据库注册表，支持**运行时纳管/吊销**（扩展版只读 config.yaml）；
- 内置事件广播（node.online / node.offline），供内核与插件订阅；
- 只做控制面，不含轮询监控（那是 manager 的职责）。

安全边界：命令在节点侧按白名单执行（见 `agent.py`）；hub 侧只投递、不解释语义。
"""
import asyncio
import hmac
import json
import logging
import struct
import threading
import time

from service.transport.framed import FrameCodec, HEADER_LEN, DEFAULT_REPLAY_WINDOW
from service.transport.crypto import SecureCodec
from service.transport.mux import (
    F, FrameRouter, StreamMux,
    pack_stream, unpack_stream, pack_flow, unpack_flow,
)

logger = logging.getLogger('zernus')

# 帧类型（首字节）——别名自 service/transport/mux.F，保持对外导出名不变
F_HELLO = F.HELLO
F_HEARTBEAT = F.HEARTBEAT
F_CMD = F.CMD
F_RESULT = F.RESULT
F_ERROR = F.ERROR
F_SUB = F.SUB
F_UNSUB = F.UNSUB
F_STDOUT = F.STDOUT
F_STDIN = F.STDIN
F_FLOW = F.FLOW

_READ_TIMEOUT = 90.0        # 握手 / 读帧兜底超时（秒），需远大于心跳周期
_MAX_PAYLOAD = 1024 * 1024  # 单帧载荷上限（控制面只传 JSON，不需要大载荷）


class NodeHub:
    """hub 侧控制通道：接受节点外连、聚合心跳、下发命令。"""

    def __init__(self, fw, registry, host: str = '0.0.0.0', port: int = 37010,
                 path: str = '', log=None):
        """hub 侧控制面。

        **监听方式三选一，且允许完全不监听**：
        - 配了 `path` → 只监听 Unix domain socket，**不占任何 TCP 端口**
          （Windows 10+ / Linux 均支持）；
        - `port > 0` → 监听 TCP；
        - `port <= 0` 且无 path → **不监听**（进程照常运行，不报错）。

        最后一种是有意保留的：面板端可能只做「出向连接」或由外部通道接管，
        此时不该因为它没有监听端口就起不来。
        """
        self.fw = fw
        self.registry = registry
        self.host = host
        self.port = int(port)
        self.path = str(path or '').strip()
        self._log = log or (lambda m: logger.info(m))

        self.conns = {}                 # name -> conn dict
        self._lock = threading.Lock()   # 保护 conns / pending
        self._pending = {}              # cmd id -> future
        self._cmd_seq = 0

        # 数据面：一条 TCP 上多路复用实例输出流。首字节分区后，流帧走这里，
        # 不经 Web API —— 大流量因此不占 HTTP 通道，也无需像 MCSM 那样旁路直连。
        self.stream_mux = StreamMux()
        self._stream_callbacks = {}     # stream_id -> [callback]
        self._flow_windows = {}         # node -> {stream_id: 已授予字节数}

        # 首字节分发表：读到 type 即决定走哪条路径（控制面立即处理，流面走多路复用）
        self.router = FrameRouter()
        self.router.on(F.HEARTBEAT, self._h_heartbeat)
        self.router.on(F.RESULT, self._h_result)
        self.router.on(F.STDOUT, self._h_stdout)
        self.router.on(F.FLOW, self._h_flow)
        self.router.on(F.ERROR, self._h_error)

        self._loop = None
        self._server = None
        self._stop_evt = None
        self._thread = None
        self._running = False

    # ── 生命周期 ──────────────────────────────────────────
    def start(self):
        """在独立线程里跑 asyncio 事件循环（不与内核主循环互相阻塞）。"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop,
                                        name='node-hub', daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._loop and self._stop_evt:
            try:
                self._loop.call_soon_threadsafe(self._stop_evt.set)
            except Exception:
                pass

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_evt = asyncio.Event()
        try:
            self._loop.run_until_complete(self._serve())
        except Exception:
            logger.exception("[nodes.hub] 服务循环异常退出")
        finally:
            self._loop.close()
            self._loop = None

    async def _serve(self):
        # 监听方式优先级：unix socket（不占端口）→ TCP → 完全不监听
        if self.path:
            import os
            import socket as _socket
            if not hasattr(_socket, 'AF_UNIX'):
                logger.warning(
                    "[nodes.hub] 当前平台的 Python 不提供 AF_UNIX（%s），已忽略 path=%r。"
                    "无端口部署请改用 port<=0（完全不监听），或换 Unix 平台。",
                    __import__('sys').platform, self.path)
                return
            try:
                if os.path.exists(self.path):
                    os.unlink(self.path)          # 清理上次异常退出残留的 socket 文件
                # 不用 asyncio.start_unix_server —— 它在 Windows 上不存在。
                # 自行 bind 后交给 loop.create_server，Unix / Windows 10+ 统一可用。
                sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
                sock.bind(self.path)
                sock.listen(128)
                sock.setblocking(False)
                self._server = await asyncio.get_running_loop().create_server(
                    self._on_conn, sock=sock)
                self._log(f"节点控制面监听 unix socket {self.path}（不占 TCP 端口）")
            except Exception as e:
                logger.error(f"[nodes.hub] unix socket 监听失败（{self.path}）: {e}")
                return
        elif self.port > 0:
            try:
                self._server = await asyncio.start_server(self._on_conn,
                                                          self.host, self.port)
                self._log(f"节点控制面监听 {self.host}:{self.port}")
            except OSError as e:
                logger.error(f"[nodes.hub] 端口 {self.port} 监听失败: {e}")
                return
        else:
            self._log("节点控制面未监听任何端点（未配置端口 / socket）——保持出向能力")
            return

        try:
            await self._stop_evt.wait()
        finally:
            try:
                self._server.close()
                await self._server.wait_closed()
            except Exception:
                pass
            if self.path:
                import os
                try:
                    if os.path.exists(self.path):
                        os.unlink(self.path)
                except Exception:
                    pass

    # ── 帧读写 ────────────────────────────────────────────
    @staticmethod
    async def _read_frame(reader) -> bytes:
        head = await asyncio.wait_for(reader.readexactly(HEADER_LEN), _READ_TIMEOUT)
        plen = struct.unpack_from(">I", head, 21)[0]
        if plen > _MAX_PAYLOAD:
            raise ValueError(f"载荷超限 {plen}B")
        payload = await asyncio.wait_for(reader.readexactly(plen),
                                         _READ_TIMEOUT) if plen else b""
        return head + payload

    async def _on_conn(self, reader, writer):
        peer = writer.get_extra_info('peername')
        peer_str = f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) else str(peer)
        name = None
        try:
            # ── 握手：首帧必须是 HELLO，按节点名取密钥验签 ──
            raw = await self._read_frame(reader)
            hello = FrameCodec(b'probe').decode(raw)   # 仅解头部，token 稍后校验
            if hello.type != F_HELLO:
                raise ValueError('首帧非 HELLO')
            info = json.loads(hello.payload.decode('utf-8', 'replace'))
            name = str(info.get('name', '')).strip()
            secret = self.registry.secrets_map().get(name)
            if not secret:
                raise ValueError(f'未知节点 {name!r}（未纳管或已吊销）')
            codec = SecureCodec(secret, direction='s2c',
                                replay_window=DEFAULT_REPLAY_WINDOW)
            expect = codec.make_token(hello.ts, hello.type, hello.seq, hello.payload)
            if not hmac.compare_digest(hello.token, expect):
                raise ValueError(f'节点 {name!r} 握手验签失败')

            # 取连接 nonce 并启用载荷加密。HELLO 自身是明文（nonce 要靠它传），
            # 但它受 HMAC 保护——篡改 nonce 会让后续所有帧解不出来，等价于握手失败。
            nonce_hex = str(info.get('nonce') or '')
            if nonce_hex:
                try:
                    codec.set_nonce(bytes.fromhex(nonce_hex))
                except Exception:
                    raise ValueError(f'节点 {name!r} 的 nonce 非法')

            conn = {
                'writer': writer, 'codec': codec, 'last_seq': hello.seq,
                'version': str(info.get('version', '')),
                'platform': str(info.get('platform', '')),
                'encrypted': codec.encrypted,
                'status': dict(info), 'last_seen_at': time.time(), 'peer': peer_str,
            }
            with self._lock:
                old = self.conns.get(name)
                self.conns[name] = conn
            if old is not None:                     # 同名新连接顶掉旧连接
                try:
                    old['writer'].close()
                except Exception:
                    pass
            self._log(f"节点 {name} ({peer_str}) 已接入控制面 v{conn['version'] or '?'}")
            self._broadcast('node.online', {'name': name, 'peer': peer_str,
                                            'version': conn['version'],
                                            'platform': conn['platform']})
            # 上线即落库，避免面板等到下一次心跳才有状态
            try:
                self.registry.save_status(name, 'online', {
                    'version': conn['version'], 'uptime_seconds': 0, 'memory_mb': None})
            except Exception:
                pass

            # ── 帧循环 ──
            while True:
                raw = await self._read_frame(reader)
                fr = codec.decode(raw)                      # 载荷仍是密文
                ok, reason = codec.verify_seq(fr, conn['last_seq'])   # 对密文验签
                if not ok:
                    logger.warning("[nodes.hub] 丢弃来自 %s 的帧: %s", name, reason)
                    continue
                codec.open(fr)                              # 验签通过 → 解密
                conn['last_seq'] = fr.seq
                conn['last_seen_at'] = time.time()
                # 首字节分发：控制帧立即处理，流帧进多路复用 —— 互不排队
                await self.router.dispatch(name, fr, writer)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError,
                ConnectionError, ValueError, OSError, json.JSONDecodeError) as e:
            if name:
                self._log(f"节点 {name} 连接断开: {e}")
            elif not isinstance(e, asyncio.IncompleteReadError):
                logger.warning("[nodes.hub] 未完成握手的连接 %s 断开: %s", peer_str, e)
        finally:
            if name:
                with self._lock:
                    if self.conns.get(name, {}).get('writer') is writer:
                        self.conns.pop(name, None)
                self._log(f"节点 {name} 已离线")
                try:
                    self.registry.save_status(name, 'offline')
                except Exception:
                    pass
                self._broadcast('node.offline', {'name': name})
            try:
                writer.close()
            except Exception:
                pass

    # ── 事件广播（内核事件总线；无总线时静默降级）──
    def _broadcast(self, event: str, payload: dict):
        bus = getattr(self.fw, 'event_bus', None)
        if bus is None:
            return
        try:
            inner = bus.emit(event, payload)
            loop = getattr(self.fw, 'loop', None)
            if loop is not None and loop.is_running():
                asyncio.run_coroutine_threadsafe(inner, loop)
        except Exception:
            logger.debug("[nodes.hub] 事件广播失败: %s", event)

    # ── 帧处理器（首字节分发的落点）────────────────────────
    async def _h_heartbeat(self, peer, frame, writer):
        conn = self.conns.get(peer)
        if conn is None:
            return
        try:
            conn['status'] = json.loads(frame.text())
            self.registry.save_status(peer, 'online', conn['status'])
        except Exception:
            logger.debug("[nodes.hub] 心跳载荷解析失败 from %s", peer)

    async def _h_result(self, peer, frame, writer):
        try:
            res = json.loads(frame.text())
            with self._lock:
                fut = self._pending.pop(int(res.get('id', -1)), None)
            if fut and not fut.done():
                fut.set_result({'ok': bool(res.get('ok')), 'data': res.get('data')})
        except Exception:
            logger.exception("[nodes.hub] RESULT 处理异常")

    async def _h_stdout(self, peer, frame, writer):
        """实例输出上行。这是数据面的主干：日志/控制台流走这里，不经 Web API。"""
        sid, data = unpack_stream(frame.payload)
        if not sid:
            return
        ep = self.stream_mux.get(sid)
        if ep is None:
            return                      # 无人订阅：直接丢弃，不缓冲无主数据
        if not ep.push(data):
            self._send_frame(peer, F.FLOW, pack_flow(sid, 0))   # 溢出 → 让节点暂停
            return
        if ep.paused:
            self._send_frame(peer, F.FLOW, pack_flow(sid, 0))   # 到高水位 → 暂停
        for cb in list(self._stream_callbacks.get(sid, [])):
            try:
                cb(peer, sid, data)
            except Exception:
                logger.exception("[nodes.hub] 流回调异常 stream=%s", sid)

    async def _h_flow(self, peer, frame, writer):
        """节点侧回传的流控窗口：用于控制 hub → 节点（stdin / 命令）的下行速率。"""
        sid, window = unpack_flow(frame.payload)
        conn = self.conns.get(peer)
        if conn is not None:
            conn.setdefault('flow', {})[sid] = int(window)
        logger.debug("[nodes.hub] 节点 %s 流控 stream=%s window=%s", peer, sid, window)

    async def _h_error(self, peer, frame, writer):
        logger.warning("[nodes.hub] 节点 %s 报错: %s", peer, frame.text()[:200])

    # ── 帧发送 / 数据面接口 ────────────────────────────────
    def _send_frame(self, node: str, ftype: int, payload: bytes) -> bool:
        """向节点发送一帧（同步写；drain 由事件循环负责）。"""
        with self._lock:
            conn = self.conns.get(node)
        if conn is None:
            return False
        try:
            seq = int(time.time() * 1000) & 0x7FFFFFFF
            conn['writer'].write(conn['codec'].encode(ftype, int(time.time()), seq, payload))
            return True
        except Exception as e:
            logger.debug("[nodes.hub] 发送帧失败 node=%s type=0x%02X: %s", node, ftype, e)
            return False

    def subscribe(self, node: str, stream_id: str, callback=None) -> dict:
        """订阅节点上的一条实例输出流。

        :param callback: `(node, stream_id, data)`，收到数据块时调用（同步函数）
        """
        self.stream_mux.add_subscriber(stream_id, callback or node)
        if callback is not None:
            self._stream_callbacks.setdefault(stream_id, []).append(callback)
        sent = self._send_frame(node, F.SUB, pack_stream(stream_id)) if self.is_online(node) else False
        return {'ok': True, 'stream': stream_id, 'node': node,
                'node_online': bool(sent)}

    def unsubscribe(self, node: str, stream_id: str, callback=None) -> dict:
        """退订：移除本地订阅者，并在无人订阅时通知节点停止推流。"""
        if callback is not None:
            lst = self._stream_callbacks.get(stream_id, [])
            if callback in lst:
                lst.remove(callback)
            if not lst:
                self._stream_callbacks.pop(stream_id, None)
        self.stream_mux.remove_subscriber(stream_id, callback or node)
        if self.stream_mux.get(stream_id) is None:
            self._send_frame(node, F.UNSUB, pack_stream(stream_id))
        return {'ok': True, 'stream': stream_id}

    def write_stdin(self, node: str, stream_id: str, data: bytes) -> dict:
        """把数据写入节点上实例的标准输入（下行，量小）。"""
        if not self.is_online(node):
            return {'ok': False, 'data': f'节点 {node} 不在线'}
        if not self._send_frame(node, F.STDIN, pack_stream(stream_id, data)):
            return {'ok': False, 'data': '发送失败'}
        return {'ok': True, 'bytes': len(data)}

    def stream_stats(self) -> dict:
        """数据面观测：流数 / 缓冲 / 丢弃 / 暂停。"""
        return self.stream_mux.stats()

    # ── 对外接口（线程安全，任意线程可调）──
    def snapshot(self) -> list:
        """当前在线节点快照（不做网络请求）。"""
        with self._lock:
            return [{'name': n, 'peer': c['peer'], 'version': c['version'],
                     'platform': c['platform'],
                     'encrypted': bool(c.get('encrypted')),
                     'online': True,
                     'last_seen_at': time.strftime('%Y-%m-%d %H:%M:%S',
                                                   time.localtime(c['last_seen_at'])),
                     'status': dict(c['status'])}
                    for n, c in sorted(self.conns.items())]

    def is_online(self, name: str) -> bool:
        with self._lock:
            return name in self.conns

    def online_names(self) -> list:
        with self._lock:
            return sorted(self.conns.keys())

    def send_cmd(self, name: str, cmd: str, args: dict = None,
                 timeout: float = 30.0) -> dict:
        """向在线节点下发命令并等待回执。

        :return: {'ok': bool, 'data': ...}；节点离线 / 超时 / 拒绝时 ok=False
        """
        if not self._loop:
            return {'ok': False, 'data': '控制面未启动'}
        fut = asyncio.run_coroutine_threadsafe(
            self._send_cmd_coro(name, cmd, args or {}, timeout), self._loop)
        try:
            return fut.result(timeout + 10)
        except Exception as e:
            return {'ok': False, 'data': f'{type(e).__name__}: {e}'}

    async def _send_cmd_coro(self, name, cmd, args, timeout):
        with self._lock:
            conn = self.conns.get(name)
            if conn is None:
                return {'ok': False, 'data': f'节点 {name} 不在线'}
            self._cmd_seq += 1
            cid = self._cmd_seq
            fut = self._loop.create_future()
            self._pending[cid] = fut
        try:
            payload = json.dumps({'id': cid, 'cmd': cmd, 'args': args},
                                 ensure_ascii=False).encode('utf-8')
            seq = int(time.time() * 1000) & 0x7FFFFFFF
            conn['writer'].write(conn['codec'].encode(F_CMD, int(time.time()), seq, payload))
            await conn['writer'].drain()
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return {'ok': False, 'data': f'命令回执超时（{timeout}s）'}
        except (ConnectionError, OSError) as e:
            return {'ok': False, 'data': f'连接异常: {e}'}
        finally:
            with self._lock:
                self._pending.pop(cid, None)

    def kick(self, name: str) -> dict:
        """主动断开某节点的控制连接（吊销 / 踢下线）。"""
        with self._lock:
            conn = self.conns.get(name)
        if conn is None:
            return {'ok': False, 'data': f'节点 {name} 不在线'}
        try:
            conn['writer'].close()
            return {'ok': True, 'data': f'节点 {name} 已断开'}
        except Exception as e:
            return {'ok': False, 'data': str(e)}
