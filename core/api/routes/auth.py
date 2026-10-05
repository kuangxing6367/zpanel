# -*- coding: utf-8 -*-
"""认证接口：登录 / 登出 / 当前用户 / 改密 / 用户管理 / 接口令牌。

角色：super（全权）> admin（日常运维）> viewer（只读白名单命令）。
用户管理与令牌签发是 super 专属 —— 认证只回答「是谁」，这里管「谁能干什么」。
"""
import logging

from flask import request

logger = logging.getLogger('zernus')


def register(app, ctx):
    auth_svc = ctx.auth

    @app.route('/api/auth/login', methods=['POST'])
    def _login():
        data = request.get_json(silent=True) or {}
        username = str(data.get('username') or '').strip()
        password = str(data.get('password') or '')
        if not username or not password:
            return ctx.fail('请输入用户名和密码', 400)
        res = auth_svc.login(username, password, ctx.client_ip())
        if not res.get('ok'):
            return ctx.fail(res.get('error', '登录失败'), 401)
        # must_change_password：仍在用出厂默认密码 → 前端应引导改密（后端另有写操作闸门）
        return ctx.ok({'token': res['token'], 'user': res['user'],
                       'must_change_password': bool(res.get('must_change_password'))})

    @app.route('/api/auth/logout', methods=['POST'])
    def _logout():
        token = ctx.bearer_token()
        if token:
            auth_svc.logout(token)
        return ctx.ok({'logged_out': True})

    @app.route('/api/auth/me', methods=['GET'])
    @ctx.require_auth
    def _me():
        u = dict(request.zp_user)
        u['must_change_password'] = auth_svc.is_using_default_password(u['username'])
        return ctx.ok({'user': u})

    @app.route('/api/auth/password', methods=['POST'])
    @ctx.require_auth
    def _change_password():
        data = request.get_json(silent=True) or {}
        res = auth_svc.change_password(request.zp_user['username'],
                                       str(data.get('old_password') or ''),
                                       str(data.get('new_password') or ''))
        if not res.get('ok'):
            return ctx.fail(res.get('error', '修改失败'), 400)
        return ctx.ok({'changed': True})

    # ── 用户管理（super 专属）────────────────────────────
    @app.route('/api/auth/users', methods=['GET'])
    @ctx.require_auth(role='super')
    def _list_users():
        return ctx.ok({'users': auth_svc.list_users()})

    @app.route('/api/auth/users', methods=['POST'])
    @ctx.require_auth(role='super')
    def _create_user():
        data = request.get_json(silent=True) or {}
        res = auth_svc.create_user(str(data.get('username') or ''),
                                   str(data.get('password') or ''),
                                   role=str(data.get('role') or 'admin'))
        if not res.get('ok'):
            return ctx.fail(res.get('error', '创建失败'), 400)
        ctx.audit('user.create', 'user', res.get('username', ''),
                  detail={'role': res.get('role')})
        return ctx.ok(res)

    @app.route('/api/auth/users/<name>', methods=['PATCH'])
    @ctx.require_auth(role='super')
    def _update_user(name):
        data = request.get_json(silent=True) or {}
        res = auth_svc.update_user(
            name,
            role=data.get('role'),
            is_active=(None if data.get('is_active') is None
                       else bool(data.get('is_active'))),
            actor=request.zp_user['username'])
        if not res.get('ok'):
            return ctx.fail(res.get('error', '更新失败'), 400)
        ctx.audit('user.update', 'user', name, detail=data)
        return ctx.ok(res)

    @app.route('/api/auth/users/<name>', methods=['DELETE'])
    @ctx.require_auth(role='super')
    def _delete_user(name):
        res = auth_svc.delete_user(name, actor=request.zp_user['username'])
        if not res.get('ok'):
            return ctx.fail(res.get('error', '删除失败'), 400)
        ctx.audit('user.delete', 'user', name)
        return ctx.ok(res)

    # ── 接口令牌（API Key）──
    @app.route('/api/auth/tokens', methods=['GET'])
    @ctx.require_auth
    def _list_tokens():
        return ctx.ok({'tokens': auth_svc.list_api_tokens()})

    @app.route('/api/auth/tokens', methods=['POST'])
    @ctx.require_auth(role='super')
    def _create_token():
        data = request.get_json(silent=True) or {}
        res = auth_svc.issue_api_token(
            name=str(data.get('name') or 'default'),
            role=str(data.get('role') or 'admin'),
            created_by=request.zp_user['username'],
            expires_at=data.get('expires_at'))
        return ctx.ok(res)          # token 明文仅此一次返回

    @app.route('/api/auth/tokens/<int:tid>', methods=['DELETE'])
    @ctx.require_auth(role='super')
    def _revoke_token(tid):
        return ctx.ok(auth_svc.revoke_api_token(tid))
