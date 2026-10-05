# -*- coding: utf-8 -*-
"""认证接口：登录 / 登出 / 当前用户 / 改密 / 接口令牌。"""
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
        return ctx.ok({'token': res['token'], 'user': res['user']})

    @app.route('/api/auth/logout', methods=['POST'])
    def _logout():
        token = ctx.bearer_token()
        if token:
            auth_svc.logout(token)
        return ctx.ok({'logged_out': True})

    @app.route('/api/auth/me', methods=['GET'])
    @ctx.require_auth
    def _me():
        return ctx.ok({'user': request.zp_user})

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

    # ── 接口令牌（API Key）──
    @app.route('/api/auth/tokens', methods=['GET'])
    @ctx.require_auth
    def _list_tokens():
        return ctx.ok({'tokens': auth_svc.list_api_tokens()})

    @app.route('/api/auth/tokens', methods=['POST'])
    @ctx.require_auth
    def _create_token():
        data = request.get_json(silent=True) or {}
        res = auth_svc.issue_api_token(
            name=str(data.get('name') or 'default'),
            role=str(data.get('role') or 'admin'),
            created_by=request.zp_user['username'],
            expires_at=data.get('expires_at'))
        return ctx.ok(res)          # token 明文仅此一次返回

    @app.route('/api/auth/tokens/<int:tid>', methods=['DELETE'])
    @ctx.require_auth
    def _revoke_token(tid):
        return ctx.ok(auth_svc.revoke_api_token(tid))
