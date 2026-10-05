# -*- coding: utf-8 -*-
"""
节点代理 · 被控端（内核级多机管理 · 节点侧）

跑在每台被管机器上：主动外连 hub 的控制面（节点可在 NAT / 防火墙后，
无需公网入向端口），握手注册 → 周期心跳 → 接收命令 → 回执。

**本模块不含任何系统环境逻辑**——内核不管系统环境。它只做两件事：

1. 心跳：调用 `fw.nodes.collect()`，把**服务层与软件层注册上来的数据**
   原样上报（内核不解释、不加工）；
2. 命令：收到 `cmd` 后交给 `fw.nodes.execute()`，由**注册了该命令的层**
   去执行（谁注册、谁实现、谁负责）。

传输复用服务层 `service/transport/framed.py`；密钥为 per-node 预共享密钥，
须与 hub 侧 `NodeRegistry` 中该节点的记录一致。
"""
import asyncio
import json
import logging
import platform
import threading
import time

from service.transport.framed import FramedClient, HEADER_LEN, DEFAULT_REPLAY_WINDOW
from service.transport.crypto import SecureCodec, new_nonce
from .registry import derive_key

logger = logging.getLogger('zernus')

F_HELLO = 1
F_HEARTBEAT = 2
F_CMD = 3
F_RESULT = 4


class NodeAgent:
    """节点侧代理：维持到 hub 的长连接，上报数据 + 转发命令。

    `hub_path` 非空时走 Unix domain socket —— 不经 TCP、不占端口，
    与服务端「允许没有端口」相对应。
    """

    def __init__(self, fw, name: str, secret: str, hub_host: str,
                 hub_port: int = 37010, interval: int = 30,
                 hub_path: str = '', log=None):
        self.fw = fw
        self.name = str(name or '').strip()
        # 原始密钥 → 派生通讯密钥。派生方式与 hub 侧 secrets_map() 完全一致，
        # 因此两边无需交换「实际密钥」，只需 node name + 同一份配置密钥即可对上。
        self.secret = derive_key(secret, self.name) if (secret and self.name) else b''
        self.hub_host = hub_host
        self.hub_port = int(hub_port)
        self.hub_path = str(hub_path or '').strip()
        self.interval = max(5, int(interval))
        self._log = log or (lambda m: logger.info(m))

        self._seq = int(time.time()) & 0x7FFFFFFF
        # 连接级 nonce：握手时随 HELLO 明文带给 hub，之后双向载荷一律加密。
        # 它保证「换一条连接就换一段 keystream」，与帧内单调 seq 共同杜绝密钥流复用。
        self._nonce = new_nonce(16)
        self._codec = None
        self._last_rx_seq = 0          # 接收方向序号（用于对密文验签）
        self._loop = None
        self._thread = None
        self._client = None
        self._task = None
        self._connected = False

    # ── 上报数据（全部来自各层注册的数据源，内核不加工）──
    def _payload(self) -> dict:
        try:
            data = self.fw.nodes.collect()
        except Exception as e:
            logger.debug("[nodes.agent] 采集数据失败: %s", e)
            data = {}
        return {
            'node': self.name,
            'version': _version(self.fw),
            'platform': f"{platform.system()} {platform.release()}",
            'python': platform.python_version(),
            'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
            'data': data,       # ← 服务层 / 软件层提供的数据
        }

    @property
    def connected(self) -> bool:
        return self._connected

    # ── 连接循环 ──────────────────────────────────────────
    def start(self):
        if not self.name or not self.secret:
            self._log("节点代理未启动：name / secret 未配置")
            return
        self._thread = threading.Thread(target=self._run_loop,
                                        name='node-agent', daemon=True)
        self._thread.start()

    def stop(self):
        self._connected = False
        if self._loop and self._task:
            try:
                self._loop.call_soon_threadsafe(self._task.cancel)
            except Exception:
                pass

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._task = self._loop.create_task(self._forever())
            self._loop.run_until_complete(self._task)
        except (asyncio.CancelledError, RuntimeError):
            pass
        except Exception:
            logger.exception("[nodes.agent] 代理循环异常退出")
        finally:
            self._loop.close()
            self._loop = None

    async def _forever(self):
        backoff = 2
        while True:
            try:
                # 每条连接换一个新 nonce —— keystream 绝不跨连接复用
                self._nonce = new_nonce(16)
                self._codec = None
                self._client = FramedClient(self.secret,
                                            replay_window=DEFAULT_REPLAY_WINDOW)
                if self.hub_path:       # 无端口模式：走 socket 文件
                    await self._client.connect_unix(self.hub_path, timeout=10)
                    self._connected = True
                    self._log(f"已连接 hub（unix socket {self.hub_path}）")
                else:
                    await self._client.connect(self.hub_host, self.hub_port, timeout=10)
                    self._connected = True
                    self._log(f"已连接 hub {self.hub_host}:{self.hub_port}")
                backoff = 2
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[nodes.agent] 连接异常: %s，%ss 后重连", e, backoff)
            finally:
                self._connected = False
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(backoff * 2, 120)

    async def _session(self):
        # 握手前先装 codec：此时**未设 nonce**，载荷仍是明文，但帧认证已改用
        # K_mac（= HMAC(master,'mac')）。hub 侧验签用的是同一把 K_mac，
        # 两边必须一致，否则握手必败。
        self._codec = SecureCodec(self.secret, direction='c2s',
                                  replay_window=DEFAULT_REPLAY_WINDOW)
        self._client._codec = self._codec

        # ── HELLO：明文发出（nonce 要靠它传），整帧受 HMAC 保护 ──
        self._next_seq()
        await self._client.send(F_HELLO, self._seq, json.dumps({
            'name': self.name, 'version': _version(self.fw),
            'platform': f"{platform.system()} {platform.release()}",
            'nonce': self._nonce.hex(),
        }, ensure_ascii=False).encode('utf-8'))

        # ── 握手完成 → 启用载荷加密（此后收发自动加解密）──
        self._codec.set_nonce(self._nonce)

        last_hb = 0.0
        while True:
            now = time.time()
            wait = max(0.5, self.interval - (now - last_hb))
            frame = None
            try:
                frame = await asyncio.wait_for(self._recv_frame(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                self._log("与 hub 的连接已断开")
                return

            now = time.time()
            if now - last_hb >= self.interval:      # 到点上报
                self._next_seq()
                await self._client.send(F_HEARTBEAT, self._seq,
                                        json.dumps(self._payload(),
                                                   ensure_ascii=False).encode('utf-8'))
                last_hb = now
            if frame is None:
                continue
            if frame.type == F_CMD:
                res = await self._dispatch(frame)
                self._next_seq()
                await self._client.send(F_RESULT, self._seq,
                                        json.dumps(res, ensure_ascii=False).encode('utf-8'))

    async def _dispatch(self, frame) -> dict:
        """把 hub 命令交给内核路由；执行放到线程池，避免阻塞事件循环。"""
        try:
            req = json.loads(frame.text())
        except Exception as e:
            return {'id': -1, 'ok': False, 'data': f'命令载荷解析失败: {e}'}
        cid = int(req.get('id', -1))
        cmd = str(req.get('cmd', ''))
        args = req.get('args') or {}
        try:
            res = await asyncio.to_thread(self.fw.nodes.execute, cmd, args)
            res = dict(res or {})
        except Exception as e:
            res = {'ok': False, 'data': f'{type(e).__name__}: {e}'}
        res['id'] = cid
        return res

    async def _recv_frame(self):
        """读一整帧：解析 → 对密文验签 → 解密。

        顺序不能颠倒：发送端 token 是对密文算的，先解密再验签必然失败。
        """
        import struct as _s
        reader = getattr(self._client, '_reader', None)
        if reader is None:
            raise ConnectionError("未连接")
        head = await reader.readexactly(HEADER_LEN)
        plen = _s.unpack_from(">I", head, 21)[0]
        if plen > 1024 * 1024:
            raise ValueError(f"载荷超限 {plen}B")
        payload = await reader.readexactly(plen) if plen else b""
        codec = self._client._codec
        fr = codec.decode(head + payload)
        ok, reason = codec.verify_seq(fr, self._last_rx_seq)
        if not ok:
            logger.warning("[nodes.agent] 丢弃来自 hub 的帧: %s", reason)
            return None
        self._last_rx_seq = fr.seq
        return codec.open(fr)

    def _next_seq(self):
        self._seq = (self._seq + 1) & 0x7FFFFFFF
        return self._seq


def _version(fw) -> str:
    """读项目根 VERSION 文件（失败返回空串，不影响心跳）。"""
    try:
        from core.kernel.paths import project_root
        import os
        with open(os.path.join(project_root(), 'VERSION'), encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return ''
