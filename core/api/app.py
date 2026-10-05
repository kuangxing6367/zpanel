# -*- coding: utf-8 -*-
"""
内核 Web API 装配（core/api · app）

    create_api_app(fw) -> Flask     构建应用（鉴权中间件 + 路由 + 前端托管）
    ApiServer(fw)                   在独立线程中运行 Web 服务器
    ApiContext                      路由上下文：统一响应 / 鉴权 / 审计 / 挂接口

内核这里是「界面出口」：它自己带鉴权、自己托管前端、自己暴露核心数据接口，
不依赖任何扩展。各层要加自己的接口，用 `ctx.register_api(...)` 挂进来即可。
"""
import functools
import logging
import os
import threading
import time

from flask import Flask, jsonify, request

from .security import AuthService
from . import static as static_mod
from . import routes as routes_mod

logger = logging.getLogger('zernus')

# 不在审计里落库的读取型命令/接口（前端轮询每 2-8s 一次，全记就是日志洪水）。
# 写操作与显式动作（install/uninstall/act/…）永远审计。
AUDIT_SKIP = {
    'ping', 'node.info', 'sysres.snapshot', 'sysres.top', 'monitor.history',
    'task.list', 'task.get', 'files.list', 'files.roots', 'files.stat',
    'sites.list', 'sites.status', 'sites.php-versions', 'sites.render',
    'runtime.list', 'runtime.logs', 'scheduler.list', 'scheduler.backends',
    'scheduler.validate', 'backup.list', 'alerts.list', 'alerts.events',
    'logs.list', 'db.list', 'db.status', 'db.databases', 'db.tables',
    'db.version', 'db.compat', 'db.services', 'db.processlist',
    'firewall.status', 'firewall.rules', 'svc.list', 'ssl.local', 'ssl.check',
    'sites.adopt.scan', 'db.compat', 'db.scan',
}

# viewer 角色可用的命令白名单（只读集合）—— 写命令一律拒绝
READONLY_CMDS = frozenset(AUDIT_SKIP | {
    'files.read', 'db.query', 'logs.tail', 'logs.search', 'runtime.compat',
    'runtime.detect', 'runtime.env', 'runtime.template', 'runtime.versions',
    'db.scan', 'db.test', 'ssl.read', 'files.hash', 'files.disk',
})

# 角色层级：super > admin > viewer
_ROLE_LEVEL = {'viewer': 0, 'admin': 1, 'super': 2}


def _role_at_least(user_role: str, need: str) -> bool:
    return _ROLE_LEVEL.get(str(user_role or 'viewer'), 0) >= _ROLE_LEVEL.get(need, 1)


class ApiContext:
    """内核 API 的请求上下文：响应封装 / 鉴权 / 审计 / 接口挂载。"""

    def __init__(self, fw, app=None):
        self.fw = fw
        self.app = app
        self.auth = AuthService(fw)
        self._ext_counter = 0

    # ── 统一响应 ──────────────────────────────────────────
    @staticmethod
    def ok(data=None, status=200):
        payload = {'ok': True}
        if isinstance(data, dict):
            payload.update(data)
        elif data is not None:
            payload['data'] = data
        return jsonify(payload), status

    @staticmethod
    def fail(error: str, status=400):
        return jsonify({'ok': False, 'error': error}), status

    # ── 请求上下文 ────────────────────────────────────────
    def client_ip(self) -> str:
        """真实客户端 IP。

        X-Forwarded-For **只信任来自可信代理的连接**（security.trusted_proxies，
        默认空 = 不信任任何 XFF）—— 否则登录限速按伪造 IP 计数，形同虚设。
        """
        fwd = request.headers.get('X-Forwarded-For', '')
        if fwd:
            trusted = (self.fw.config.get('security') or {}).get('trusted_proxies') or []
            remote = request.remote_addr or ''
            if remote in trusted or '*' in trusted:
                return fwd.split(',')[0].strip()
        return request.remote_addr or ''

    @staticmethod
    def bearer_token() -> str:
        """从 Authorization / X-API-Key / Cookie 中取令牌。"""
        h = request.headers.get('Authorization', '')
        if h.lower().startswith('bearer '):
            return h[7:].strip()
        return (request.headers.get('X-API-Key')
                or request.cookies.get('zp_token') or '')

    # ── 鉴权装饰器 ────────────────────────────────────────
    def require_auth(self, fn=None, role: str = None):
        """要求登录（会话令牌或接口令牌均可）。校验通过后 `request.zp_user` 可用。

        两种用法：`@ctx.require_auth` 直接装饰；`@ctx.require_auth(role='super')`
        要求最低角色（viewer < admin < super）。用户管理/令牌签发/配置写回/
        节点纳管这类路由应传 role='super'。
        认证通过后还有一道**默认口令闸门**：仍用出厂密码的账号，
        除改密/登录/只读（GET）外的一切操作都拒绝 —— 面板装完的第一件事
        就该是改密码，写操作在此之前一律不放行。
        """
        def deco(f):
            @functools.wraps(f)
            def wrapper(*a, **kw):
                token = self.bearer_token()
                user = self.auth.verify_token(token) or self.auth.verify_api_token(token)
                if not user:
                    return self.fail('未登录或登录已过期', 401)
                if role and not _role_at_least(user.get('role'), role):
                    return self.fail(f'权限不足（需要 {role} 角色）', 403)
                request.zp_user = user
                request.zp_token = token
                if (request.method != 'GET'
                        and request.path not in ('/api/auth/logout', '/api/auth/password',
                                                 '/api/auth/me')
                        and self.auth.is_using_default_password(user['username'])):
                    # 命令通道的只读命令放行（监控查询/收养扫描这类 POST 只读也要能用）
                    if request.path.endswith('/cmd'):
                        body = request.get_json(silent=True) or {}
                        if str(body.get('cmd') or '') in READONLY_CMDS:
                            return f(*a, **kw)
                    return self.fail('仍在使用默认密码：请先修改密码（POST /api/auth/password，'
                                     '新密码至少 8 位）再执行该操作', 403)
                return f(*a, **kw)
            return wrapper

        return deco(fn) if fn is not None else deco

    # ── 审计 ──────────────────────────────────────────────
    def audit(self, action: str, target_type: str = '', target_name: str = '',
              detail: dict = None, result: str = 'success'):
        """写审计日志（失败不影响主流程）。

        轮询类只读命令在 AUDIT_SKIP 里跳过；写操作与显式动作永远审计。
        每写入若干条顺手清理超过保留期（log.audit_retention_days，默认 30 天）
        的旧行 —— 与 monitor 历史同一个「采集即清理」纪律，审计表不再无限膨胀。
        """
        try:
            if action in AUDIT_SKIP:
                return
            import json
            user = getattr(request, 'zp_user', None) or {}
            self.fw.db.execute(
                "INSERT INTO audit_logs (admin_id, admin_name, action, target_type, "
                "target_name, detail, ip_address, result, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user.get('id'), user.get('username', ''), action, target_type,
                 target_name, json.dumps(detail or {}, ensure_ascii=False)[:2000],
                 self.client_ip(), result, time.strftime('%Y-%m-%d %H:%M:%S')))
            self._audit_count = getattr(self, '_audit_count', 0) + 1
            if self._audit_count % 200 == 0:
                days = int((self.fw.config.get('log') or {}).get(
                    'audit_retention_days', 30) or 30)
                cutoff = time.strftime('%Y-%m-%d %H:%M:%S',
                                       time.localtime(time.time() - days * 86400))
                self.fw.db.execute("DELETE FROM audit_logs WHERE created_at < ?", (cutoff,))
        except Exception as e:
            logger.debug("[api] 审计写入失败: %s", e)

    # ── 供各层挂接口 ──────────────────────────────────────
    def register_route(self, path: str, methods, handler, auth: bool = True):
        """兼容既有注册契约（`ctx.register_api` 走的就是这个签名）。

        handler 直接作为 Flask 视图挂载——这样内核 API 可以作为 `fw.api_registry`
        承载各层注册的接口，上层无需感知底层是内核还是扩展。
        """
        if self.app is None:
            return None
        self._ext_counter += 1
        endpoint = f'zp_ext_{self._ext_counter}'
        methods = list(methods or ['GET'])
        view = handler if not auth else self.require_auth(handler)
        try:
            self.app.add_url_rule(path, endpoint=endpoint, view_func=view,
                                  methods=methods)
            logger.debug("[api] 已挂载接口 %s %s", path, methods)
            return {'path': path, 'methods': tuple(methods), 'endpoint': endpoint}
        except Exception as e:
            logger.error("[api] 挂载接口 %s 失败: %s", path, e)
            return None

    def register_api(self, path: str, handler, methods=None, auth: bool = True,
                     description: str = ''):
        """把一条自定义接口挂到内核 API 上（复用内核鉴权）。

        handler 契约：`handler(**url_params) -> dict | (status, dict)`
        """
        if self.app is None:
            return False
        methods = [m.upper() for m in (methods or ['GET'])]
        self._ext_counter += 1
        endpoint = f'zp_ext_{self._ext_counter}'
        view = handler if not auth else self.require_auth(handler)

        @functools.wraps(view)
        def _view(*a, **kw):
            res = view(*a, **kw)
            if isinstance(res, tuple) and len(res) == 2 and isinstance(res[1], (dict, list)):
                status, body = res
                return jsonify({'ok': 200 <= int(status) < 300, **({'data': body}
                                if not isinstance(body, dict) else body)}), status
            if isinstance(res, dict):
                return jsonify({'ok': True, **res})
            return jsonify({'ok': True, 'data': res})

        try:
            self.app.add_url_rule(path, endpoint=endpoint, view_func=_view,
                                  methods=methods)
            logger.debug("[api] 已挂载接口 %s %s %s", path, methods, description)
            return True
        except Exception as e:
            logger.error("[api] 挂载接口 %s 失败: %s", path, e)
            return False


def create_api_app(fw, ctx: ApiContext = None) -> Flask:
    """构建内核 Web 应用（含鉴权、路由、前端托管）。"""
    from core.kernel.paths import project_root
    root = project_root()

    app = Flask(__name__, static_folder=None)
    try:
        app.json.ensure_ascii = False       # 中文直出，便于调试
    except Exception:
        app.config['JSON_AS_ASCII'] = False
    # 请求体上限（默认 64MB：文件分块上传 base64 有余量，内存 DoS 被闸住）
    try:
        app.config['MAX_CONTENT_LENGTH'] = int(
            (fw.config.get('api') or {}).get('max_body_mb', 64) or 64) * 1024 * 1024
    except Exception:
        app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024

    ctx = ctx or ApiContext(fw, app)
    ctx.app = app

    # 审计时间索引（保留清理与按时间查询都靠它；幂等，启动建一次）
    try:
        fw.db.execute("CREATE INDEX IF NOT EXISTS idx_audit_created "
                      "ON audit_logs (created_at)")
    except Exception as e:
        logger.debug("[api] 审计索引创建失败: %s", e)

    # ── CORS：**只对显式白名单放行** ──
    # 空白名单 = 不加任何 CORS 头（同源部署天然可用）。
    # 之前「空 = 反射任意 Origin + 带凭据」，任意网站都能带着用户身份跨域调 API。
    allowed = [o for o in ((fw.config.get('api') or {}).get('cors_origins') or []) if o]

    @app.after_request
    def _cors(resp):
        origin = request.headers.get('Origin', '')
        if origin and origin in allowed:
            resp.headers['Access-Control-Allow-Origin'] = origin
            resp.headers['Access-Control-Allow-Credentials'] = 'true'
            resp.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type, X-API-Key'
            resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PATCH, DELETE, OPTIONS'
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        return resp

    @app.route('/api/<path:_any>', methods=['OPTIONS'])
    def _preflight(_any):
        return ('', 204)

    # ── 业务路由 ──
    routes_mod.register_all(app, ctx)

    # ── 首次自举：确保存在管理员账号 ──
    try:
        ctx.auth.ensure_default_admin()
    except Exception as e:
        logger.error("[api] 管理员自举失败: %s", e)

    # ── 各层在 Web 启动前登记的接口（ctx.register_api 的缓冲）──
    #     扩展/插件先于 Web 加载，它们的路由先落缓冲，这里统一挂上。
    pending = list(getattr(fw, '_pending_api_routes', None) or [])
    for item in pending:
        try:
            path, methods, handler, auth = item
            ctx.register_route(path, methods, handler, auth)
        except Exception as e:
            logger.error("[api] 挂载缓冲路由失败 %s: %s", item[0] if item else '?', e)
    if pending:
        logger.info("[api] 已挂载 %d 条各层登记的接口", len(pending))
    try:
        fw._pending_api_routes = []
    except Exception:
        pass

    # ── 实时推送：内核能力，零新增依赖 ──────────────────────
    #     ① StreamHub 挂到 fw.stream，任何层都能 publish(topic, event, data)；
    #     ② 票据接口走普通鉴权（token 在 header 里），换出的一次性票据才上 URL；
    #     ③ WebSocket 服务随 ApiServer.start() 一起起（见下）。
    #     路由必须在前端托管之前注册（后者是 catch-all，会吃掉 /api/stream/*）。
    try:
        from core.api import stream as _stream_mod
        from core.api import ws as _ws_mod
        from flask import request as _sreq

        if getattr(fw, 'stream', None) is None:
            fw.stream = _stream_mod.StreamHub(log=lambda m: logger.debug(m))

        def _ticket():
            body = _sreq.get_json(silent=True) or {}
            topic = str(body.get('topic') or '').strip()
            if not topic:
                return ctx.fail('topic 不能为空')
            t = fw.stream.new_ticket(topic, int(body.get('ttl') or 0))
            # 告诉前端 WS 端点在哪：主机名沿用浏览器访问 API 用的那个（同机部署）
            try:
                host = _sreq.host.rsplit(':', 1)[0] or '127.0.0.1'
            except Exception:
                host = '127.0.0.1'
            t['ws_url'] = f"ws://{host}:{fw.ws_stream.port}"
            return ctx.ok(t)

        def _stats():
            return ctx.ok(fw.stream.stats())

        ctx.register_route('/api/stream/ticket', ['POST'], _ticket, True)
        ctx.register_route('/api/stream/stats', ['GET'], _stats, True)

        wcfg = fw.config.get('ws') or {}
        # 注意：fw.ws_server 是 engine 的只读 property（services 遗留），不能赋值
        # —— 本机栽过一次（AttributeError 被外层 except 吞掉，表现为换票 500）。
        fw.ws_stream = _ws_mod.WSStreamServer(
            fw.stream,
            host=str(wcfg.get('host', '127.0.0.1')),
            port=int(wcfg.get('port', 8001)),
            check_ticket=fw.stream.check_ticket,
            allowed_origins=(wcfg.get('origins') or
                             ((fw.config.get('api') or {}).get('cors_origins') or [])),
            log=lambda m: logger.info(m))
    except Exception as e:
        logger.error("[api] 实时推送初始化失败: %s", e, exc_info=True)

    # ── 前端托管（必须最后注册：catch-all 兜底）──
    static_mod.register_static(app, fw, root)

    app.zp_ctx = ctx
    return app


class ApiServer:
    """内核 Web 服务器（独立线程运行，不与内核事件循环互相阻塞）。"""

    def __init__(self, fw, ctx: ApiContext = None):
        self.fw = fw
        cfg = (fw.config.get('api') or {}) if isinstance(fw.config, dict) else {}
        self.host = str(cfg.get('host', '127.0.0.1'))
        self.port = int(cfg.get('port', 8000))
        self.app = create_api_app(fw)
        self.ctx = self.app.zp_ctx
        self._thread = None
        self._server = None

    def start(self):
        # 实时推送（WebSocket）与 Web API 一起起：端口独立，互不干扰
        ws = getattr(self.fw, 'ws_stream', None)
        if ws is not None:
            try:
                ws.start()
            except Exception as e:
                logger.error(f"[api] 实时推送启动失败: {e}")
        self._thread = threading.Thread(target=self._run, daemon=True, name='zp-api')
        self._thread.start()

    def _run(self):
        try:
            try:
                from waitress.server import create_server
                self._server = create_server(self.app, host=self.host, port=self.port,
                                             threads=8)
                logger.info(f"[api] 内核 Web API 已启动: http://{self.host}:{self.port}")
                self._server.run()
            except ImportError:
                from werkzeug.serving import make_server
                self._server = make_server(self.host, self.port, self.app, threaded=True)
                logger.info(f"[api] 内核 Web API 已启动（werkzeug）: "
                            f"http://{self.host}:{self.port}")
                self._server.serve_forever()
        except Exception as e:
            if 'Address already in use' in str(e) or getattr(e, 'errno', None) == 98:
                logger.error(f"[api] 端口 {self.port} 被占用，内核 Web API 未启动")
            else:
                logger.error(f"[api] 内核 Web API 异常: {e}")

    def stop(self):
        ws = getattr(self.fw, 'ws_stream', None)
        if ws is not None:
            try:
                ws.stop()
            except Exception:
                pass
        srv = self._server
        self._server = None
        if srv is not None:
            try:
                if hasattr(srv, 'close'):
                    srv.close()
                elif hasattr(srv, 'shutdown'):
                    srv.shutdown()
            except Exception as e:
                logger.warning(f"[api] 关闭异常: {e}")
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
