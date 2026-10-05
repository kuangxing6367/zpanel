# -*- coding: utf-8 -*-
"""
内核鉴权（core/api · 认证与令牌）

零第三方依赖：密码哈希用 stdlib `hashlib.pbkdf2_hmac`，令牌用 `secrets`。
复用框架既有表结构，不另建库：

    admin_users  管理员账号（username / password_hash / token / role）
    api_tokens   接口令牌（外部程序调用 REST API 用）

设计要点：
- **明文不落库**：登录令牌在库中只存 `sha256(token)`，明文仅在签发时返回一次；
- **恒定时间比对**：密码与令牌校验均用 `hmac.compare_digest`，避免时序侧信道；
- **首次自举**：库中无管理员时自动创建默认账号并打日志强提示改密。
"""
import hashlib
import hmac
import json
import logging
import os
import secrets
import time

logger = logging.getLogger('zernus')

_ITER = 200_000
_PREFIX = 'pbkdf2_sha256'
DEFAULT_ADMIN = 'admin'
DEFAULT_PASSWORD = 'admin123'


def hash_password(password: str) -> str:
    """生成 pbkdf2_sha256 哈希串：`pbkdf2_sha256$<iter>$<salt_hex>$<hash_hex>`"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, _ITER)
    return f"{_PREFIX}${_ITER}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码（恒定时间比对；格式非法一律返回 False）。"""
    if not stored:
        return False
    if stored.startswith(_PREFIX + '$'):
        try:
            _, iter_s, salt_hex, hash_hex = stored.split('$', 3)
            dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                     bytes.fromhex(salt_hex), int(iter_s))
            return hmac.compare_digest(dk.hex(), hash_hex)
        except Exception:
            return False
    return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def new_token() -> str:
    """签发一个 URL 安全的会话令牌。"""
    return secrets.token_urlsafe(32)


class AuthService:
    """登录 / 令牌 / 口令 —— 内核级认证服务。"""

    def __init__(self, fw):
        self.fw = fw
        self._login_fails = {}      # ip -> [count, first_ts]，简单登录限速
        self._max_fails = 10
        self._fail_window = 300

    # ── 账号 ──────────────────────────────────────────────
    def ensure_default_admin(self) -> bool:
        """库中无管理员时创建默认账号（首次启动自举）。"""
        try:
            n = self.fw.db.scalar("SELECT COUNT(*) FROM admin_users") or 0
        except Exception:
            n = 0
        if n:
            return False
        self.create_user(DEFAULT_ADMIN, DEFAULT_PASSWORD, role='super')
        logger.warning("=" * 62)
        logger.warning("  已创建默认管理员账号: %s / %s   ← 请立即修改密码！",
                       DEFAULT_ADMIN, DEFAULT_PASSWORD)
        logger.warning("=" * 62)
        return True

    def create_user(self, username: str, password: str, role: str = 'admin') -> dict:
        username = str(username or '').strip()
        if not username:
            return {'ok': False, 'error': '用户名不能为空'}
        if self.fw.db.query_one("SELECT id FROM admin_users WHERE username=?", (username,)):
            return {'ok': False, 'error': f'用户 {username} 已存在'}
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        self.fw.db.execute(
            "INSERT INTO admin_users (username, password_hash, role, is_active, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (username, hash_password(password), role, now))
        return {'ok': True, 'username': username, 'role': role}

    def get_user(self, username: str) -> dict:
        row = self.fw.db.query_one("SELECT * FROM admin_users WHERE username=?", (username,))
        return dict(row) if row else None

    def list_users(self) -> list:
        rows = self.fw.db.query(
            "SELECT id, username, role, is_active, last_login_at, last_login_ip, created_at "
            "FROM admin_users ORDER BY id") or []
        return [dict(r) for r in rows]

    # ── 限速 ──────────────────────────────────────────────
    def _too_many_fails(self, ip: str) -> bool:
        rec = self._login_fails.get(ip)
        if not rec:
            return False
        count, first = rec
        if time.time() - first > self._fail_window:
            self._login_fails.pop(ip, None)
            return False
        return count >= self._max_fails

    def _record_fail(self, ip: str):
        rec = self._login_fails.get(ip)
        if not rec or time.time() - rec[1] > self._fail_window:
            self._login_fails[ip] = [1, time.time()]
        else:
            rec[0] += 1

    # ── 登录 / 登出 ───────────────────────────────────────
    def login(self, username: str, password: str, ip: str = '') -> dict:
        """校验口令并签发令牌；返回 {'ok', 'token', 'user'} 或 {'ok': False, 'error'}。"""
        if ip and self._too_many_fails(ip):
            return {'ok': False, 'error': '尝试过于频繁，请稍后再试'}

        user = self.get_user(str(username or '').strip())
        if not user or not user.get('is_active'):
            if ip:
                self._record_fail(ip)
            return {'ok': False, 'error': '用户名或密码错误'}
        if not verify_password(password, user.get('password_hash') or ''):
            if ip:
                self._record_fail(ip)
            return {'ok': False, 'error': '用户名或密码错误'}

        token = new_token()
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        try:
            self.fw.db.execute(
                "UPDATE admin_users SET token=?, token_created_at=?, last_login_at=?, "
                "last_login_ip=? WHERE id=?",
                (_token_hash(token), now, now, ip or '', user['id']))
        except Exception as e:
            logger.error("[api.auth] 写入登录令牌失败: %s", e)
            return {'ok': False, 'error': '登录状态写入失败'}
        if ip:
            self._login_fails.pop(ip, None)
        logger.info("[api.auth] 用户 %s 登录成功（%s）", user['username'], ip or 'unknown')
        return {'ok': True, 'token': token,
                'user': {'username': user['username'], 'role': user.get('role', 'admin')}}

    def logout(self, token: str) -> dict:
        h = _token_hash(token or '')
        try:
            self.fw.db.execute(
                "UPDATE admin_users SET token=NULL, token_created_at=NULL WHERE token=?", (h,))
        except Exception as e:
            return {'ok': False, 'error': str(e)}
        return {'ok': True}

    def verify_token(self, token: str) -> dict:
        """校验会话令牌；返回用户 dict 或 None。"""
        if not token:
            return None
        try:
            row = self.fw.db.query_one(
                "SELECT id, username, role, is_active, token_created_at FROM admin_users "
                "WHERE token=?", (_token_hash(token),))
        except Exception:
            return None
        if not row or not row.get('is_active'):
            return None
        # 会话有效期（默认 12 小时，可在 config.api.session_timeout 覆盖）
        ttl = int((self.fw.config.get('api') or {}).get('session_timeout', 43200) or 43200)
        created = row.get('token_created_at')
        if ttl > 0 and created:
            try:
                ts = time.mktime(time.strptime(str(created)[:19], '%Y-%m-%d %H:%M:%S'))
                if time.time() - ts > ttl:
                    return None
            except Exception:
                pass
        return {'id': row['id'], 'username': row['username'],
                'role': row.get('role', 'admin')}

    # ── 改密 ──────────────────────────────────────────────
    def change_password(self, username: str, old_password: str, new_password: str) -> dict:
        user = self.get_user(username)
        if not user:
            return {'ok': False, 'error': '用户不存在'}
        if not verify_password(old_password, user.get('password_hash') or ''):
            return {'ok': False, 'error': '原密码错误'}
        if len(str(new_password or '')) < 6:
            return {'ok': False, 'error': '新密码至少 6 位'}
        self.fw.db.execute(
            "UPDATE admin_users SET password_hash=?, token=NULL WHERE id=?",
            (hash_password(new_password), user['id']))
        return {'ok': True}

    # ── 接口令牌（API Key）────────────────────────────────
    def issue_api_token(self, name: str, role: str = 'admin',
                        created_by: str = '', expires_at: str = None) -> dict:
        """签发接口令牌（供外部程序调用 REST API）。明文仅返回一次。"""
        token = 'zp_' + secrets.token_urlsafe(40)
        self.fw.db.execute(
            "INSERT INTO api_tokens (token, name, role, created_by, created_at, expires_at, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?, 1)",
            (_token_hash(token), str(name or 'default'), role, created_by,
             str(int(time.time())), expires_at))
        return {'ok': True, 'token': token, 'name': name, 'role': role}

    def verify_api_token(self, token: str) -> dict:
        """校验接口令牌；命中则刷新 last_used_at。"""
        if not token:
            return None
        try:
            row = self.fw.db.query_one(
                "SELECT * FROM api_tokens WHERE token=? AND is_active=1", (_token_hash(token),))
        except Exception:
            return None
        if not row:
            return None
        exp = row.get('expires_at')
        if exp:
            try:
                if int(exp) < time.time():
                    return None
            except Exception:
                pass
        try:
            self.fw.db.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?",
                               (str(int(time.time())), row['id']))
        except Exception:
            pass
        return {'id': row['id'], 'username': f"token:{row.get('name')}",
                'role': row.get('role', 'admin'), 'via': 'api_token'}

    def list_api_tokens(self) -> list:
        rows = self.fw.db.query(
            "SELECT id, name, role, created_by, created_at, expires_at, last_used_at, is_active "
            "FROM api_tokens ORDER BY id DESC") or []
        return [dict(r) for r in rows]

    def revoke_api_token(self, tid: int) -> dict:
        self.fw.db.execute("UPDATE api_tokens SET is_active=0 WHERE id=?", (int(tid),))
        return {'ok': True}
