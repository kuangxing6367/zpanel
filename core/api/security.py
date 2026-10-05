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

_ITER = 600_000                 # OWASP 2024+ 建议；旧哈希登录时透明升级
_PREFIX = 'pbkdf2_sha256'
DEFAULT_ADMIN = 'admin'
DEFAULT_PASSWORD = 'admin123'


def hash_password(password: str) -> str:
    """生成 pbkdf2_sha256 哈希串：`pbkdf2_sha256$<iter>$<salt_hex>$<hash_hex>`"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, _ITER)
    return f"{_PREFIX}${_ITER}${salt.hex()}${dk.hex()}"


def needs_rehash(stored: str) -> bool:
    """旧迭代次数的哈希需要升级（登录成功时透明重哈希）。"""
    try:
        return int(str(stored).split('$', 3)[1]) < _ITER
    except Exception:
        return False


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
        self._default_pwd_cache = {}    # username -> (bool, ts)：默认口令判定有 600k 轮 pbkdf2，别每请求都算

    def is_using_default_password(self, username: str) -> bool:
        """该账号是否仍在用出厂默认密码（结果缓存 5 分钟，改密即失效）。"""
        import time as _t
        cached = self._default_pwd_cache.get(username)
        if cached and _t.time() - cached[1] < 300:
            return cached[0]
        user = self.get_user(username)
        ok = bool(user) and verify_password(DEFAULT_PASSWORD, user.get('password_hash') or '')
        self._default_pwd_cache[username] = (ok, _t.time())
        return ok

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
        if role not in ('super', 'admin', 'viewer'):
            return {'ok': False, 'error': f'非法角色: {role}'}
        if len(str(password or '')) < 8:
            return {'ok': False, 'error': '密码至少 8 位'}
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        self.fw.db.execute(
            "INSERT INTO admin_users (username, password_hash, role, is_active, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (username, hash_password(password), role, now))
        return {'ok': True, 'username': username, 'role': role}

    def update_user(self, username: str, *, role: str = None,
                    is_active: bool = None, actor: str = '') -> dict:
        """改角色/启停。**最后一个 super 不可降级或停用** —— 不然面板把自己锁死。"""
        user = self.get_user(username)
        if not user:
            return {'ok': False, 'error': '用户不存在'}
        if role is not None and role not in ('super', 'admin', 'viewer'):
            return {'ok': False, 'error': f'非法角色: {role}'}
        supers = self.fw.db.scalar(
            "SELECT COUNT(*) FROM admin_users WHERE role='super' AND is_active=1") or 0
        demoting = (role is not None and role != 'super' and user.get('role') == 'super')
        deactivating = (is_active is False and user.get('role') == 'super')
        if user.get('is_active') and user.get('role') == 'super' and supers <= 1 \
                and (demoting or deactivating):
            return {'ok': False, 'error': '最后一个 super 账号不可降级/停用'}
        sets, vals = [], []
        if role is not None:
            sets.append('role=?'); vals.append(role)
        if is_active is not None:
            sets.append('is_active=?'); vals.append(1 if is_active else 0)
            if not is_active:
                sets.append('token=NULL')       # 停用即踢下线
        if not sets:
            return {'ok': False, 'error': '没有要修改的字段'}
        vals.append(username)
        self.fw.db.execute(f"UPDATE admin_users SET {', '.join(sets)} WHERE username=?", vals)
        logger.info("[api.auth] 用户 %s 已由 %s 更新", username, actor)
        return {'ok': True}

    def delete_user(self, username: str, actor: str = '') -> dict:
        if username == actor:
            return {'ok': False, 'error': '不能删除当前登录的账号'}
        user = self.get_user(username)
        if not user:
            return {'ok': False, 'error': '用户不存在'}
        if user.get('role') == 'super':
            supers = self.fw.db.scalar(
                "SELECT COUNT(*) FROM admin_users WHERE role='super' AND is_active=1") or 0
            if supers <= 1:
                return {'ok': False, 'error': '最后一个 super 账号不可删除'}
        self.fw.db.execute("DELETE FROM admin_users WHERE username=?", (username,))
        logger.info("[api.auth] 用户 %s 已由 %s 删除", username, actor)
        return {'ok': True}

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
        # 旧迭代次数的哈希在登录成功时透明升级（用户无感，安全性单调提升）
        try:
            if needs_rehash(user.get('password_hash') or ''):
                self.fw.db.execute("UPDATE admin_users SET password_hash=? WHERE id=?",
                                   (hash_password(password), user['id']))
                logger.info("[api.auth] 用户 %s 密码哈希已升级到 %d 轮",
                            user['username'], _ITER)
        except Exception as e:
            logger.warning("[api.auth] 哈希升级失败: %s", e)
        logger.info("[api.auth] 用户 %s 登录成功（%s）", user['username'], ip or 'unknown')
        return {'ok': True, 'token': token,
                'must_change_password': self.is_using_default_password(user['username']),
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
        if len(str(new_password or '')) < 8:
            return {'ok': False, 'error': '新密码至少 8 位'}
        if str(new_password) == DEFAULT_PASSWORD:
            return {'ok': False, 'error': '新密码不能与出厂默认密码相同'}
        self.fw.db.execute(
            "UPDATE admin_users SET password_hash=?, token=NULL WHERE id=?",
            (hash_password(new_password), user['id']))
        self._default_pwd_cache.pop(username, None)     # 门禁立即放行
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
