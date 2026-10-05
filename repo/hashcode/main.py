"""hashcode —— 哈希摘要与编解码（机制包）。

框架提供的一等公民程序化接口：生成摘要、base64/hex/url 编解码、
短 ID / UUID，避免各处 import 一堆标准库再手写包装。

稳定 API 表面：
    sha256(data) / sha1(data) / md5(data) -> hex 字符串
    b64encode(data) / b64decode(s) -> str
    hex_encode(data) / hex_decode(s) -> str
    urlencode(s) / urldecode(s) -> str
    short_id(n=8) -> str                随机短 ID（hex 字符）
    uuid4() -> str

设计约定：data 为 str 时按 encoding（默认 utf-8）编码，为 bytes 直接用。
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import urllib.parse
import uuid


def _to_bytes(data, encoding: str = "utf-8") -> bytes:
    return data.encode(encoding) if isinstance(data, str) else bytes(data)


def sha256(data, encoding: str = "utf-8") -> str:
    return hashlib.sha256(_to_bytes(data, encoding)).hexdigest()


def sha1(data, encoding: str = "utf-8") -> str:
    return hashlib.sha1(_to_bytes(data, encoding)).hexdigest()


def md5(data, encoding: str = "utf-8") -> str:
    return hashlib.md5(_to_bytes(data, encoding)).hexdigest()


def b64encode(data, encoding: str = "utf-8") -> str:
    return base64.b64encode(_to_bytes(data, encoding)).decode("ascii")


def b64decode(s: str, encoding: str = "utf-8") -> str:
    return base64.b64decode(s.encode("ascii")).decode(encoding)


def hex_encode(data, encoding: str = "utf-8") -> str:
    return _to_bytes(data, encoding).hex()


def hex_decode(s: str, encoding: str = "utf-8") -> str:
    return bytes.fromhex(s).decode(encoding)


def urlencode(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def urldecode(s: str) -> str:
    return urllib.parse.unquote(s)


def short_id(n: int = 8) -> str:
    """返回 n 位随机十六进制字符串（默认 8 位）。"""
    return secrets.token_hex((n + 1) // 2)[:n]


def uuid4() -> str:
    return str(uuid.uuid4())
