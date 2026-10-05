# -*- coding: utf-8 -*-
"""
secretbox — 可逆加密（零框架依赖，纯标准库）

用途：把**必须取回明文**的凭据（数据库口令、第三方 API Key…）落盘保存。
和 `hashcode` 的分工很清楚：

- `hashcode`：单向摘要，用来**比对**（口令对不对）。
- `secretbox`：双向加密，用来**保管**（以后还要拿明文去连数据库）。

## 构造（Encrypt-then-MAC，这是必须的顺序）

```
nonce   = 随机 16 字节（每次加密都换，绝不复用）
K_enc   = HMAC-SHA256(master, b"enc")
K_mac   = HMAC-SHA256(master, b"mac")
ks      = SHAKE256(K_enc ‖ nonce)          NIST 标准 XOF，一次展开
cipher  = plain XOR ks                     XOR 自反，加解密同一函数
tag     = HMAC-SHA256(K_mac, nonce ‖ cipher)
blob    = b64(nonce ‖ cipher ‖ tag)
```

三个容易被写错、这里特意规避的点：

1. **先加密再认证**（Encrypt-then-MAC）。反过来（MAC-then-Encrypt 或 Encrypt-and-MAC）
   在填充/流式场景下都出过事。认证的是**密文**，解密前先验 tag，tag 不对直接拒绝，
   绝不解出明文让调用方拿到可能是伪造的数据。
2. **同一把 master 不跨用途复用**：`K_enc`、`K_mac` 由它派生而来，用途分离。
3. **nonce 每次随机、随文存储**：不随文存就解不开，复用就泄密钥流。

## 威胁边界（说清楚，别让调用方误用）

- 它保护的是「落盘的库文件被拷走」这类场景：**没有 master 就解不开**。
- 它**不保护**运行时内存、也不保护能同时读到库文件和 master 的人 —— 那种情况下
  任何软件层加密都没用（等同于把钥匙和锁放一个抽屉）。master 应与库分开存放。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

__version__ = "1.0.0"

NONCE_LEN = 16
TAG_LEN = 32


def new_key() -> bytes:
    """生成一个新的主密钥（32 字节随机）。"""
    return os.urandom(32)


def _split(master: bytes) -> tuple:
    return (hmac.new(master, b"enc", hashlib.sha256).digest(),
            hmac.new(master, b"mac", hashlib.sha256).digest())


def seal(plaintext, key: bytes) -> str:
    """加密。接受 str / bytes，返回可安全存进数据库的文本。

    明文为空时返回空串（不是加密空串）—— 空凭据就是没有凭据，不需要保密。
    """
    if plaintext is None:
        return ""
    data = plaintext.encode("utf-8") if isinstance(plaintext, str) else bytes(plaintext)
    if not data:
        return ""
    k_enc, k_mac = _split(key)
    nonce = os.urandom(NONCE_LEN)
    ks = hashlib.shake_256(k_enc + nonce).digest(len(data))
    cipher = bytes(a ^ b for a, b in zip(data, ks))
    tag = hmac.new(k_mac, nonce + cipher, hashlib.sha256).digest()
    return base64.b64encode(nonce + cipher + tag).decode("ascii")


class InvalidToken(ValueError):
    """tag 校验失败：密钥不对，或数据被改过。**绝不返回部分明文**。"""


def open_sealed(blob: str, key: bytes) -> str:
    """解密。tag 不对抛 InvalidToken，绝不吐出可能是伪造的明文。"""
    if not blob:
        return ""
    try:
        raw = base64.b64decode(blob.encode("ascii"), validate=False)
    except Exception as e:
        # 存坏了 / 被截断 / 不是本包的格式 —— 一律当成无效凭据，不区分具体原因
        raise InvalidToken(f"密文格式非法: {e}") from None
    if len(raw) < NONCE_LEN + TAG_LEN:
        raise InvalidToken("密文长度不足")
    nonce, body = raw[:NONCE_LEN], raw[NONCE_LEN:]
    cipher, tag = body[:-TAG_LEN], body[-TAG_LEN:]
    k_enc, k_mac = _split(key)

    # 先验 tag：不通过就到此为止，绝不进行解密
    expect = hmac.new(k_mac, nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expect):
        raise InvalidToken("认证失败：密钥不匹配或密文被篡改")

    ks = hashlib.shake_256(k_enc + nonce).digest(len(cipher))
    plain = bytes(a ^ b for a, b in zip(cipher, ks))
    return plain.decode("utf-8", "replace")


def load_or_create_key(path: str) -> bytes:
    """读主密钥；不存在就生成并写入（父目录自动创建）。

    文件权限：POSIX 收紧到 0600（Windows 无等价位，靠目录 ACL）。
    调用方应把它放在**与业务库不同**的位置 —— 见文件头的威胁边界。
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    try:
        with open(path, "rb") as f:
            data = f.read().strip()
        if len(data) >= 32:
            return data[:32]
    except FileNotFoundError:
        pass
    except OSError:
        pass
    key = new_key()
    with open(path, "wb") as f:
        f.write(key)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass  # Windows：没有 chmod 语义，忽略
    return key


def fingerprint(key: bytes) -> str:
    """密钥指纹（前 8 位十六进制）。用于界面显示「当前用的是哪把钥匙」，
    不泄露密钥本身。"""
    return hashlib.sha256(key).hexdigest()[:8]
