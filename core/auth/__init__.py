"""
通讯安全（Security）
===================

对应手写笔记启动流程的最后一环：

    是否加密通讯？
    ├─ 否 → 读 Token
    └─ 是 → RSA 完事 → 回调端

- 非加密模式（encrypted: false）：校验请求携带的 Token 是否与管理端配置的 token 一致。
  token 留空时视为未启用强制校验（与「读 Token」语义一致：不强制即放行）。
- 加密模式（encrypted: true）：由 RSA 握手完成后的回调端（rsa_callback）负责最终校验，
  内核只做转发占位，不内置诱捕式防护、不诱导、不拉黑。

本模块只提供「校验原语」，不含任何欺骗性防御、双请求探针、IP 黑名单等笔记之外的机制。
"""
import logging
from typing import Optional, Tuple

logger = logging.getLogger('zernus')


def is_internal_ip(ip: str) -> bool:
    """判断 IP 是否为内网/回环地址。"""
    if not ip:
        return False
    ip = ip.strip().lower()
    if ip in ('localhost', '::1', '127.0.0.1'):
        return True
    if ip.startswith('fc') or ip.startswith('fd') or ip.startswith('fe80'):
        return True
    if ip.startswith('127.') or ip.startswith('192.168.'):
        return True
    if ip.startswith('172.'):
        try:
            second = int(ip.split('.')[1])
            return 16 <= second <= 31
        except (ValueError, IndexError):
            return False
    if ip.startswith('10.'):
        return True
    return False


class AuthSystem:
    """通讯安全校验（Token / RSA 回调）。

    取代旧的双请求诱捕式防护认证系统；只实现笔记要求的两种通讯校验方式，
    不含任何笔记之外的反破解/欺骗机制。
    """

    def __init__(self, config: Optional[dict] = None, db=None):
        sec = config or {}
        # 加密通讯开关：true=RSA + 回调端；false=Token 校验
        self.encrypted = bool(sec.get('encrypted', False))
        # 非加密模式下的访问 Token（留空 = 不强制校验）
        self.token = sec.get('token') or ''
        # 加密模式：RSA 握手完成后的回调端地址
        self.rsa_callback = sec.get('rsa_callback') or ''
        self.db = db

    # ------------------------------------------------------------------
    # 核心处理
    # ------------------------------------------------------------------

    def handle_request(self, ip: str, token, nonce=None) -> Tuple[int, dict]:
        """处理 /api/auth 请求，返回 (http_status, response_dict)。

        :param ip: 客户端 IP
        :param token: 请求体中的 token（字符串）
        :param nonce: 加密模式下 RSA 握手下发的 nonce（占位透传，由回调端校验）
        """
        # 加密模式：交给回调端校验，内核只做占位
        if self.encrypted:
            return 200, {
                "code": 0,
                "msg": "RSA 握手校验交由回调端",
                "callback": self.rsa_callback or None,
                "data": {},
            }

        # 非加密模式：校验 Token
        if not self.token:
            return 200, {"code": 0, "msg": "登录成功", "data": {}}
        if isinstance(token, str) and token == self.token:
            return 200, {"code": 0, "msg": "登录成功", "data": {}}
        return 403, {"code": 403, "msg": "Token 错误"}

    # ------------------------------------------------------------------
    # 兼容性占位（供 WebUI 管理端点调用，行为均为空/放行）
    # ------------------------------------------------------------------

    def is_whitelisted(self, ip: str) -> bool:
        return False

    def is_blacklisted(self, ip: str) -> bool:
        return False

    def get_status(self) -> dict:
        return {
            "encrypted": self.encrypted,
            "token_set": bool(self.token),
            "rsa_callback": self.rsa_callback or None,
        }

    def get_blacklist(self) -> list:
        return []

    def add_manual_blacklist(self, ip: str, reason: str = "", expires_at=None) -> bool:
        return False

    def unblacklist(self, ip: str) -> bool:
        return False
