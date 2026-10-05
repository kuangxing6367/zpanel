# -*- coding: utf-8 -*-
"""
帧类型分区与首字分发（多路复用）

解决的问题：实例的输出流、文件传输这类**大流量**，如果走 Web API（HTTP + JSON）
会同时压垮面板带宽和序列化开销——MCSM 对此的解法是「让浏览器绕过面板直连节点」，
代价是每个节点都要对外暴露端口并各自配 HTTPS。

zpanel 的解法不同：把大流量**压回我们已经有的那条 TCP 长连接**。
帧头的**第一个字节就是类型**，读到首字节即可决定这条帧：

    · 归哪个处理器
    · 走控制通道还是数据通道
    · 是否需要背压控制

于是控制帧和数据帧共用一条连接、互不排队：

    0x01–0x0F  控制面    小消息，立即处理（HELLO / HEARTBEAT / CMD / RESULT）
    0x10–0x2F  流面      实例 stdout/stdin，按 stream_id 多路复用 + 背压
    0x30–0x3F  文件面    分块传输，支持断点续传

本模块只做三件事：**定类型、分发、多路复用**。帧的编解码（HMAC、防重放、
粘包拆包）仍由 `framed.py` 负责，两者职责不重叠。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Dict, Optional

logger = logging.getLogger('zernus')


# ══════════════════════════════════════════════════════════
# 一、帧类型（首字节）
# ══════════════════════════════════════════════════════════

class Region:
    """区段定义：首字节落在哪个区间，决定处理路径。"""
    CONTROL = 'control'
    STREAM = 'stream'
    FILE = 'file'
    UNKNOWN = 'unknown'


class F:
    """帧类型常量（首字节取值）。"""

    # ── 控制面 0x01–0x0F ──
    HELLO = 0x01          # 节点握手注册
    HEARTBEAT = 0x02      # 心跳 / 状态上报
    CMD = 0x03            # 命令下发
    RESULT = 0x04         # 命令回执
    ERROR = 0x05          # 错误

    # ── 流面 0x10–0x2F ──
    SUB = 0x10            # 订阅某实例的输出流
    UNSUB = 0x11          # 退订
    STDOUT = 0x12         # 实例输出数据块
    STDIN = 0x13          # 写入实例输入
    FLOW = 0x14           # 流控（背压窗口）

    # ── 文件面 0x30–0x3F ──
    FILE_OPEN = 0x30      # 打开/创建
    FILE_CHUNK = 0x31     # 数据块
    FILE_END = 0x32       # 结束
    FILE_ABORT = 0x33     # 中止


_STREAM_TYPES = {F.SUB, F.UNSUB, F.STDOUT, F.STDIN, F.FLOW}
_FILE_TYPES = {F.FILE_OPEN, F.FILE_CHUNK, F.FILE_END, F.FILE_ABORT}
_CONTROL_TYPES = {F.HELLO, F.HEARTBEAT, F.CMD, F.RESULT, F.ERROR}

# 参与失步重同步的合法类型集合（传给 FrameCodec / FramedServer）
VALID_TYPES = _CONTROL_TYPES | _STREAM_TYPES | _FILE_TYPES


def region_of(ftype: int) -> str:
    """**首字解码的核心**：一个字节 → 区段。

    这是整个分发链路上第一个、也是最便宜的判断——不需要解析帧头其余部分。
    """
    if ftype in _CONTROL_TYPES:
        return Region.CONTROL
    if ftype in _STREAM_TYPES:
        return Region.STREAM
    if ftype in _FILE_TYPES:
        return Region.FILE
    return Region.UNKNOWN


# ══════════════════════════════════════════════════════════
# 二、首字分发器
# ══════════════════════════════════════════════════════════

Handler = Callable[..., Awaitable[None]]


class FrameRouter:
    """按首字节把帧分发到处理器。

    用法：
        router = FrameRouter()
        router.on(F.CMD, on_cmd)                  # 精确类型
        router.on_region(Region.STREAM, on_stream) # 区段兜底
        await router.dispatch(peer, frame, writer)

    分发顺序：精确类型 → 区段兜底 → 默认处理器 → 丢弃（记 debug 日志）。
    处理器是 async 函数，签名 `(peer, frame, writer)`。
    """

    def __init__(self, default: Optional[Handler] = None, on_unknown=None):
        self._by_type: Dict[int, Handler] = {}
        self._by_region: Dict[str, Handler] = {}
        self._default = default
        self._on_unknown = on_unknown
        self.stats: Dict[int, int] = {}      # ftype -> 计数（观测用）

    def on(self, ftype: int, handler: Handler) -> 'FrameRouter':
        self._by_type[ftype] = handler
        return self

    def on_region(self, region: str, handler: Handler) -> 'FrameRouter':
        self._by_region[region] = handler
        return self

    async def dispatch(self, peer, frame, writer) -> None:
        ftype = frame.type
        self.stats[ftype] = self.stats.get(ftype, 0) + 1

        handler = self._by_type.get(ftype)
        if handler is None:
            handler = self._by_region.get(region_of(ftype))
        if handler is None:
            handler = self._default

        if handler is None:
            logger.debug("[mux] 无处理器，丢弃帧 type=0x%02X from %s", ftype, peer)
            if self._on_unknown:
                self._on_unknown(ftype, peer)
            return
        try:
            await handler(peer, frame, writer)
        except Exception:
            logger.exception("[mux] 处理器异常 type=0x%02X from %s", ftype, peer)


# ══════════════════════════════════════════════════════════
# 三、流多路复用 + 背压
# ══════════════════════════════════════════════════════════

class FlowControl:
    """滑动窗口式背压。

    发送端每个流持有一个窗口（可发送的字节数）。接收端消费后回发 FLOW 帧补充窗口；
    窗口耗尽则发送端挂起，直到收到补充——这样慢消费者不会把内存吃光。
    """

    def __init__(self, window: int = 512 * 1024):
        self.window = window          # 初始窗口（字节）
        self._avail = window
        self._lock = asyncio.Lock()
        self._waiter: Optional[asyncio.Future] = None

    @property
    def available(self) -> int:
        return self._avail

    def grant(self, n: int) -> None:
        """接收端补充窗口（收到 FLOW 帧时调用）。"""
        self._avail += max(0, int(n))
        w = self._waiter
        if w is not None and not w.done():
            w.set_result(True)

    async def acquire(self, n: int) -> None:
        """发送端申请 n 字节额度；不足则等待补充。"""
        while True:
            if self._avail >= n:
                self._avail -= n
                return
            loop = asyncio.get_running_loop()
            self._waiter = loop.create_future()
            try:
                await asyncio.wait_for(self._waiter, timeout=30)
            except asyncio.TimeoutError:
                # 超时兜底：放行一个最小额度，避免整条连接被一个慢消费者拖死
                logger.warning("[mux] 流控等待超时，放行最小额度")
                self._avail += n
            finally:
                self._waiter = None

    def consumed(self, n: int) -> int:
        """接收端消费后计算应回补的额度。"""
        return max(0, int(n))


class StreamEndpoint:
    """一条流的一端：环形缓冲 + 订阅者列表 + 背压。"""

    def __init__(self, stream_id: str, max_buffer: int = 256 * 1024,
                 high_water: int = 128 * 1024, low_water: int = 32 * 1024):
        self.stream_id = stream_id
        self.max_buffer = max_buffer
        self.high_water = high_water
        self.low_water = low_water

        self.buffered = 0
        self.dropped = 0
        self.subscribers: set = set()
        self.flow = FlowControl()
        self.paused = False
        self.created_at = time.time()
        self.last_active = time.time()

    def add_subscriber(self, key):
        self.subscribers.add(key)

    def remove_subscriber(self, key):
        self.subscribers.discard(key)

    def push(self, data: bytes) -> bool:
        """投递一段输出。

        超过 max_buffer 视为消费者跟不上：丢弃并计数（**不阻塞采集侧**——
        实例的 stdout 不能被面板拖死，这是与「无限缓冲」的关键取舍）。
        """
        n = len(data)
        if self.buffered + n > self.max_buffer:
            self.dropped += n
            return False
        self.buffered += n
        self.last_active = time.time()
        if self.buffered >= self.high_water and not self.paused:
            self.paused = True
            return True          # 调用方据此回发 FLOW(window=0)
        return True

    def consume(self, n: int) -> bool:
        """消费 n 字节；返回是否已从高水位回落到低水位（可恢复发送）。"""
        self.buffered = max(0, self.buffered - n)
        self.last_active = time.time()
        if self.paused and self.buffered <= self.low_water:
            self.paused = False
            return True
        return False

    def snapshot(self) -> dict:
        return {
            'stream_id': self.stream_id,
            'subscribers': len(self.subscribers),
            'buffered': self.buffered,
            'dropped': self.dropped,
            'paused': self.paused,
        }


class StreamMux:
    """流多路复用器：一条 TCP 上跑多条流。

    生命周期：
        mux.open('inst-1')            # 建立一条流
        mux.add_subscriber('inst-1', key)
        mux.push('inst-1', b'...')    # 采集侧投递输出
        mux.consume('inst-1', n)      # 消费侧确认
        mux.close('inst-1')
    """

    def __init__(self, max_buffer: int = 256 * 1024,
                 high_water: int = 128 * 1024, low_water: int = 32 * 1024):
        self.max_buffer = max_buffer
        self.high_water = high_water
        self.low_water = low_water
        self._streams: Dict[str, StreamEndpoint] = {}

    def open(self, stream_id: str) -> StreamEndpoint:
        ep = self._streams.get(stream_id)
        if ep is None:
            ep = StreamEndpoint(stream_id, self.max_buffer,
                                self.high_water, self.low_water)
            self._streams[stream_id] = ep
        return ep

    def get(self, stream_id: str) -> Optional[StreamEndpoint]:
        return self._streams.get(stream_id)

    def close(self, stream_id: str) -> None:
        self._streams.pop(stream_id, None)

    def close_all(self) -> None:
        self._streams.clear()

    def push(self, stream_id: str, data: bytes) -> bool:
        return self.open(stream_id).push(data)

    def consume(self, stream_id: str, n: int) -> bool:
        ep = self._streams.get(stream_id)
        return ep.consume(n) if ep else False

    def add_subscriber(self, stream_id: str, key) -> None:
        self.open(stream_id).add_subscriber(key)

    def remove_subscriber(self, stream_id: str, key) -> None:
        ep = self._streams.get(stream_id)
        if ep:
            ep.remove_subscriber(key)
            if not ep.subscribers:
                self.close(stream_id)

    def active(self) -> list:
        return [ep.snapshot() for ep in self._streams.values()]

    def stats(self) -> dict:
        return {
            'streams': len(self._streams),
            'buffered': sum(ep.buffered for ep in self._streams.values()),
            'dropped': sum(ep.dropped for ep in self._streams.values()),
            'paused': sum(1 for ep in self._streams.values() if ep.paused),
        }


# ══════════════════════════════════════════════════════════
# 四、流面载荷编解码
# ══════════════════════════════════════════════════════════

def pack_stream(stream_id: str, data: bytes = b'') -> bytes:
    """流面载荷：`sid_len(2) | sid(utf-8) | data`。

    stream_id 放在载荷最前面，是为了让**同一条 TCP 上的多路流**能被区分——
    帧头里没有 stream 字段，不去动既有的 29 字节帧头格式（保持向后兼容）。
    """
    sid = stream_id.encode('utf-8')[:65535]
    return len(sid).to_bytes(2, 'big') + sid + data


def unpack_stream(payload: bytes):
    """解析流面载荷，返回 (stream_id, data)。"""
    if len(payload) < 2:
        return '', b''
    n = int.from_bytes(payload[:2], 'big')
    if len(payload) < 2 + n:
        return '', payload[2:]
    return payload[2:2 + n].decode('utf-8', 'replace'), payload[2 + n:]


def pack_flow(stream_id: str, window: int) -> bytes:
    """流控载荷：`sid_len(2) | sid | window(4)`。window=0 表示暂停发送。"""
    sid = stream_id.encode('utf-8')[:65535]
    return (len(sid).to_bytes(2, 'big') + sid
            + max(0, int(window)).to_bytes(4, 'big'))


def unpack_flow(payload: bytes):
    """解析流控载荷，返回 (stream_id, window)。"""
    if len(payload) < 6:
        return '', 0
    n = int.from_bytes(payload[:2], 'big')
    sid = payload[2:2 + n].decode('utf-8', 'replace') if len(payload) >= 2 + n else ''
    off = 2 + n
    window = int.from_bytes(payload[off:off + 4], 'big') if len(payload) >= off + 4 else 0
    return sid, window
