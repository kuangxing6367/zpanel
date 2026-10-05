"""凭据存储（极简）。

镜像源默认是公开只读的（auth = none），运行时框架直接 HTTP GET 即可，
不需要任何凭据。

``CredentialStore`` 预留「凭据引用」机制：源配置里写 ``auth: ssh:139.196.237.40``
这类引用，运行时不解析（那是部署/上传工具的事）；这里只做占位与说明，
避免在框架运行时散落明文密钥。密钥文件本身由部署侧通过 SSH 私钥访问，
绝不以明文形式进入框架配置或日志。
"""

from __future__ import annotations

from typing import Optional


class CredentialStore:
    def __init__(self, mapping: Optional[dict] = None):
        # mapping: ref -> 说明性信息（不存密钥本身）
        self._mapping = mapping or {}

    def resolve(self, ref: str) -> Optional[dict]:
        """按引用名解析凭据元信息。

        返回 ``None`` 表示无需认证（公开源）。返回 dict 仅含非敏感的
        连接元信息（如协议、主机），绝不返回密钥字节/口令。
        """
        if ref in (None, "", "none"):
            return None
        return self._mapping.get(ref)
