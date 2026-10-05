# -*- coding: utf-8 -*-
"""
安全二进制帧传输原语（协议无关）。

导出：
- FrameCodec  : 定长头 + 变长载荷的编解码，HMAC 令牌 + 时间戳窗口 + 单调序号防重放。
- Frame       : 解码后的单帧数据。
- FramedServer: 基于 asyncio 的认证 TCP 帧服务端，自动处理粘包/拆包/失步重同步/超大载荷防护。
- FramedClient: 配套客户端。

以及帧类型分区层（mux）：
- F / Region / region_of : **首字节 = 帧类型**，读一个字节即知该走哪条处理路径。
- FrameRouter : 按首字节分发到处理器。
- StreamMux   : 一条 TCP 上多路复用多条流，带滑动窗口背压。

分层：`framed` 管「帧怎么安全地运」（编解码 / 鉴权 / 防重放 / 粘包），
`mux` 管「帧是什么、归谁处理、流量怎么控」。两者职责不重叠。

源自 ZCBOT / minecraftconsole 的 MC agent 协议，已剥离 MC 专属字段
（tps / players / 命令语义 / 心跳含义），保留可复用的通用传输层能力。
"""
from .framed import Frame, FrameCodec, FramedClient, FramedServer
from .crypto import SecureCodec, split_keys, new_nonce, xor_stream
from .mux import (
    F, Region, VALID_TYPES, region_of,
    FrameRouter, StreamMux, StreamEndpoint, FlowControl,
    pack_stream, unpack_stream, pack_flow, unpack_flow,
)

__all__ = [
    # 传输原语
    "FrameCodec", "Frame", "FramedServer", "FramedClient",
    # 载荷加密
    "SecureCodec", "split_keys", "new_nonce", "xor_stream",
    # 帧类型分区
    "F", "Region", "VALID_TYPES", "region_of", "FrameRouter",
    # 多路复用与背压
    "StreamMux", "StreamEndpoint", "FlowControl",
    "pack_stream", "unpack_stream", "pack_flow", "unpack_flow",
]
