# -*- coding: utf-8 -*-
"""
内核级多机管理（core/nodes）

把 zernus 原本散落在三个官方扩展里的多机能力（node_manager / node_control /
node_agent）收敛为**内核能力**，并复用服务层 `service/transport` 的安全帧传输：

    NodeRegistry  数据面：节点身份 / 凭据 / 状态（SQLite）
    NodeHub       控制面：hub 侧监听，节点主动外连，命令下发
    NodeAgent     被控端：节点侧代理，心跳 + 白名单命令执行
    NodeManager   统一外观：装配三者，挂载为 `fw.nodes`

分层：本包只做「多机」这一件事，具体运维能力（运行时 / 站点 / 文件）由
软件级模块实现，并通过 `send_cmd` 在目标节点上落地，从而天然获得多机能力。
"""
from .registry import NodeRegistry, gen_secret
from .hub import NodeHub, F_HELLO, F_HEARTBEAT, F_CMD, F_RESULT
from .agent import NodeAgent
from .manager import NodeManager

__all__ = [
    'NodeRegistry', 'gen_secret',
    'NodeHub', 'NodeAgent', 'NodeManager',
    'F_HELLO', 'F_HEARTBEAT', 'F_CMD', 'F_RESULT',
]
