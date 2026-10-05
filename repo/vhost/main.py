"""vhost —— 内置虚拟主机服务（机制包）。

存在的意义：**没装 Nginx 也能把一个站点跑起来看效果**。
注意这是「机制」，提供能力但不决定策略 —— 谁在什么端口、什么时候开，
由调用方（软件层）决定。面板**不应**默认拿它去占 80/443，那是 Nginx 的地盘。

稳定 API 表面：
    VHostServer(host='127.0.0.1', port=8080, log=None, server_name='zpanel')
        .router.set_sites(sites)    sites 为 dict 列表，字段见下
        .start() / .stop() / .running / .snapshot()
    VHostRouter().set_sites(sites) / .match(host) / .size()
    port_free(host, port) -> bool

站点 dict 字段（只认这些，多余字段忽略）：
    id       str    站点标识（日志用）
    domains  list   域名列表，支持 '*.example.com' 通配
    kind     str    'static'（默认）| 'proxy'
    root_dir str    static 时的根目录
    index    str    默认 index 文件（空格分隔多个）
    target   str    proxy 时的上游 'host:port'

设计约定：
- 静态路径用 sandbox 机制包判定（realpath + 根白名单），软链指向外部同样被拒；
- 反代强制绕过环境代理（回环地址走代理必错），并透传 X-Forwarded-For / Proto；
- 目录列表一律关闭（403），不做 fancy 索引页 —— 面板场景不需要，且易泄露。
"""
from __future__ import annotations

import http.server
import logging
import mimetypes
import os
import socket
import threading
import time
import urllib.error
import urllib.request

import zkg                                  # 由 zkg loader 注入

logger = logging.getLogger('zernus')

_MAX_BODY = 32 * 1024 * 1024        # 代理请求体上限
_CHUNK = 64 * 1024


def _sandbox():
    sb = zkg.tool("sandbox")
    if sb is None:
        raise RuntimeError("vhost 依赖 sandbox 机制包，但未被加载（检查 manifest.dependencies）")
    return sb


class VHostRouter:
    """按 Host 头匹配站点（虚拟主机路由表）。"""

    def __init__(self):
        self._exact = {}          # domain(lower) -> site
        self._wild = []           # (suffix, site)

    def set_sites(self, sites: list):
        exact, wild = {}, []
        for s in sites or []:
            if not isinstance(s, dict):
                continue
            for d in (s.get('domains') or []):
                d = str(d).strip().lower().split(':')[0]
                if not d:
                    continue
                if d.startswith('*.'):
                    wild.append((d[1:], s))        # '*.a.com' -> '.a.com'
                else:
                    exact[d] = s
        self._exact, self._wild = exact, wild

    def match(self, host: str):
        h = str(host or '').strip().lower().split(':')[0]
        if not h:
            return None
        site = self._exact.get(h)
        if site:
            return site
        for suffix, s in self._wild:
            if h.endswith(suffix):
                return s
        return None

    def size(self) -> int:
        return len(self._exact) + len(self._wild)


class VHostServer:
    """线程化 HTTP 服务：虚拟主机 + 静态 + 反代。"""

    def __init__(self, host: str = '127.0.0.1', port: int = 8080, log=None,
                 server_name: str = 'zpanel'):
        self.host = host
        self.port = int(port)
        self.server_name = server_name
        self._log = log or (lambda m: logger.info(m))
        self.router = VHostRouter()
        self._httpd = None
        self._thread = None
        self.stats = {'requests': 0, 'proxy': 0, 'static': 0, 'errors': 0,
                      'not_found': 0, 'started_at': 0}

    # ── 生命周期 ──────────────────────────────────────────
    def start(self) -> dict:
        if self._httpd is not None:
            return {'ok': True, 'note': '已在运行'}
        router, stats, sandbox = self.router, self.stats, _sandbox()
        server_name = self.server_name

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = server_name
            protocol_version = 'HTTP/1.1'

            def log_message(self, fmt, *args):      # 静默：访问日志另行处理
                pass

            def _dispatch(self, head_only=False):
                stats['requests'] += 1
                site = router.match(self.headers.get('Host', ''))
                if site is None:
                    stats['not_found'] += 1
                    self._plain(404, '没有匹配的站点（Host: %s）'
                                % self.headers.get('Host', ''))
                    return
                try:
                    if site.get('kind') == 'proxy':
                        stats['proxy'] += 1
                        self._proxy(site, head_only)
                    else:
                        stats['static'] += 1
                        self._static(site, head_only)
                except Exception as e:
                    stats['errors'] += 1
                    logger.warning("[vhost] 处理失败 site=%s: %s", site.get('id'), e)
                    self._plain(502, f'上游处理失败: {e}')

            def do_GET(self):
                self._dispatch()

            def do_HEAD(self):
                self._dispatch(head_only=True)

            def do_POST(self):
                self._dispatch()

            def do_PUT(self):
                self._dispatch()

            def do_DELETE(self):
                self._dispatch()

            def do_PATCH(self):
                self._dispatch()

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header('Allow', 'GET, HEAD, POST, PUT, DELETE, PATCH, OPTIONS')
                self.send_header('Content-Length', '0')
                self.end_headers()

            # ── 静态 ──────────────────────────────────────
            def _static(self, site, head_only):
                root = os.path.realpath(site.get('root_dir') or '.')
                raw = self.path.split('?', 1)[0]
                rel = urllib.request.url2pathname(raw).lstrip('/\\')
                # 关键：realpath 后再判白名单 —— 软链指向外部同样被拒
                path = os.path.realpath(os.path.join(root, rel))
                if not sandbox.is_within(root, path):
                    self._plain(403, '禁止访问')
                    return

                if os.path.isdir(path):
                    for idx in (site.get('index') or 'index.html').split():
                        cand = os.path.join(path, idx)
                        if os.path.isfile(cand):
                            path = cand
                            break
                    else:
                        self._plain(403, '目录列表已关闭')
                        return
                if not os.path.isfile(path):
                    self._plain(404, '文件不存在')
                    return

                ctype, _ = mimetypes.guess_type(path)
                try:
                    size = os.path.getsize(path)
                    self.send_response(200)
                    self.send_header('Content-Type', ctype or 'application/octet-stream')
                    self.send_header('Content-Length', str(size))
                    self.send_header('Cache-Control', 'no-cache')
                    self.end_headers()
                    if not head_only:
                        with open(path, 'rb') as f:
                            while True:
                                chunk = f.read(_CHUNK)
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                except Exception as e:
                    self._plain(500, f'读取失败: {e}')

            # ── 反代 ──────────────────────────────────────
            def _proxy(self, site, head_only):
                target = str(site.get('target') or '')
                if not target:
                    self._plain(502, '未配置上游')
                    return
                if ':' not in target:
                    target = target + ':80'
                length = int(self.headers.get('Content-Length') or 0)
                if length > _MAX_BODY:
                    self._plain(413, '请求体过大')
                    return
                body = self.rfile.read(length) if length else None

                req = urllib.request.Request(f'http://{target}{self.path}',
                                             data=body, method=self.command)
                for k, v in self.headers.items():
                    if k.lower() in ('host', 'connection', 'content-length',
                                     'accept-encoding', 'transfer-encoding'):
                        continue
                    req.add_header(k, v)
                req.add_header('X-Forwarded-For', self.client_address[0])
                req.add_header('X-Forwarded-Proto', 'http')
                req.add_header('Host', self.headers.get('Host', target))

                # 回环上游绝不能走环境代理
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                try:
                    with opener.open(req, timeout=30) as resp:
                        data = resp.read()
                        self.send_response(resp.status)
                        for k, v in resp.headers.items():
                            if k.lower() in ('transfer-encoding', 'connection',
                                             'content-encoding', 'content-length'):
                                continue
                            self.send_header(k, v)
                        self.send_header('Content-Length', str(len(data)))
                        self.end_headers()
                        if not head_only:
                            self.wfile.write(data)
                except urllib.error.HTTPError as e:
                    data = e.read()
                    self.send_response(e.code)
                    self.send_header('Content-Type',
                                     e.headers.get('Content-Type', 'text/plain'))
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    if not head_only:
                        self.wfile.write(data)
                except Exception as e:
                    self._plain(502, f'无法连接上游 {target}: {e}')

            # ── 工具 ──────────────────────────────────────
            def _plain(self, code, text):
                data = str(text).encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except Exception:
                    pass

        class _Server(http.server.ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        try:
            self._httpd = _Server((self.host, self.port), Handler)
        except OSError as e:
            logger.error("[vhost] 监听 %s:%s 失败: %s", self.host, self.port, e)
            return {'ok': False, 'error': str(e)}

        self.stats['started_at'] = time.time()
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name='vhost-server', daemon=True)
        self._thread.start()
        self._log(f"内置虚拟主机已启动 {self.host}:{self.port}（{self.router.size()} 个域名）")
        return {'ok': True}

    def stop(self) -> dict:
        if self._httpd is None:
            return {'ok': True}
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception as e:
            logger.warning("[vhost] 停止异常: %s", e)
        self._httpd = None
        self._thread = None
        return {'ok': True}

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def snapshot(self) -> dict:
        return {
            'running': self.running,
            'listen': f'{self.host}:{self.port}',
            'domains': self.router.size(),
            'uptime_seconds': (int(time.time() - self.stats['started_at'])
                               if self.stats['started_at'] else 0),
            **{k: v for k, v in self.stats.items() if k != 'started_at'},
        }


def port_free(host: str, port: int) -> bool:
    """探测端口是否可用（启动前给出清晰提示，而不是抛栈）。"""
    s = socket.socket()
    s.settimeout(0.4)
    try:
        probe_host = '127.0.0.1' if host in ('0.0.0.0', '::', '') else host
        return s.connect_ex((probe_host, int(port))) != 0
    finally:
        s.close()
