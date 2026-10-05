"""
协议适配器层（core.adapters）

原 framework/messaging/protocol.py（ProtocolAdapter / ActionProxy / ServiceRegistry）
已迁至 core.adapters（协议接入层是内核的稳定扩展点契约之一）。
framework/messaging/protocol.py 仅做透明重导出，调用方零改动。

对外公开：
  - ProtocolAdapter  协议适配器抽象基类（OneBot/HTTP/自定义…统一契约）
  - ActionProxy      协议中立的动作调用代理（兜底）
  - ServiceRegistry  服务注册表（核心与插件解耦点）
"""
from .protocol import ProtocolAdapter, ActionProxy, ServiceRegistry

__all__ = ['ProtocolAdapter', 'ActionProxy', 'ServiceRegistry']
