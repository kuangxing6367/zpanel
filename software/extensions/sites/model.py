# -*- coding: utf-8 -*-
"""
站点模型（Sites）

一个「站点」= 一组域名 + 一个根目录 + 一种后端处理方式：

    static  纯静态：直接把 root_dir 当 web 根
    php     走 PHP（内置 server 用 php -S 或 php-cgi；nginx 用 fastcgi_pass）
    proxy   反向代理到某个上游（通常指向本机一个 runtime 实例的端口）

**与实例的关系**：站点是「入口」，实例是「后端」。`proxy` 站点把域名流量
反代到实例端口，两者解耦——所以站点可以指向本机实例，也可以指向别的机器。

配置文件生成与运行方式是分离的：无论有没有装 Nginx，都能先生成配置；
Nginx 可用则 reload 生效，不可用则退回**内置 HTTP 服务**（零依赖、可立即验证）。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid

logger = logging.getLogger('zernus')

KIND_STATIC = 'static'
KIND_PHP = 'php'
KIND_PROXY = 'proxy'
KINDS = (KIND_STATIC, KIND_PHP, KIND_PROXY)

DEFAULT_INDEX = 'index.html index.htm index.php'


class Site:
    """单个站点。"""

    def __init__(self, cfg: dict):
        self.id = str(cfg.get('id') or uuid.uuid4().hex[:16])
        self.name = str(cfg.get('name') or '').strip() or self.id
        self.domains = _as_list(cfg.get('domains'))
        self.kind = str(cfg.get('kind') or KIND_STATIC)
        if self.kind not in KINDS:
            self.kind = KIND_STATIC
        self.root_dir = str(cfg.get('root_dir') or '').strip()
        self.target = str(cfg.get('target') or '').strip()     # proxy 上游 host:port
        self.index = str(cfg.get('index') or DEFAULT_INDEX).strip()
        self.remark = str(cfg.get('remark') or '').strip()
        self.enable_ssl = bool(cfg.get('enable_ssl'))
        self.ssl_cert = str(cfg.get('ssl_cert') or '').strip()
        self.ssl_key = str(cfg.get('ssl_key') or '').strip()
        self.php_fastcgi = str(cfg.get('php_fastcgi') or '127.0.0.1:9000').strip()
        # 站点想绑定的 PHP 版本（如 '8.2'）。空串 = 不指定，沿用 php_fastcgi 原值。
        # 一旦指定，渲染 Nginx 时按版本解出对应的 fastcgi 端点（见 _php_socket()）。
        self.php_version = str(cfg.get('php_version') or '').strip()
        self.created_at = str(cfg.get('created_at') or _now())
        self.updated_at = str(cfg.get('updated_at') or _now())

    # ── 视图 ──────────────────────────────────────────────
    def to_config(self) -> dict:
        return {
            'id': self.id, 'name': self.name, 'domains': self.domains,
            'kind': self.kind, 'root_dir': self.root_dir, 'target': self.target,
            'index': self.index, 'remark': self.remark,
            'enable_ssl': self.enable_ssl, 'ssl_cert': self.ssl_cert,
            'ssl_key': self.ssl_key, 'php_fastcgi': self.php_fastcgi,
            'php_version': self.php_version,
            'created_at': self.created_at, 'updated_at': _now(),
        }

    def update(self, data: dict) -> dict:
        for k in ('name', 'kind', 'root_dir', 'target', 'index', 'remark',
                  'ssl_cert', 'ssl_key', 'php_fastcgi', 'php_version'):
            if data.get(k) is not None:
                setattr(self, k, str(data[k]).strip())
        if data.get('domains') is not None:
            self.domains = _as_list(data['domains'])
        if data.get('enable_ssl') is not None:
            self.enable_ssl = bool(data['enable_ssl'])
        self.updated_at = _now()
        return self.to_config()

    def validate(self) -> str:
        """返回错误信息；空串表示校验通过。"""
        if not self.domains:
            return '至少填一个域名'
        if self.kind in (KIND_STATIC, KIND_PHP) and not self.root_dir:
            return '静态 / PHP 站点必须指定根目录'
        if self.kind == KIND_PHP and not self.php_fastcgi:
            return 'PHP 站点需要 fastcgi 地址'
        if self.kind == KIND_PROXY:
            if not self.target:
                return '反向代理站点必须指定上游地址'
            if ':' not in self.target:
                return '上游地址格式应为 host:port'
        if self.enable_ssl and (not self.ssl_cert or not self.ssl_key):
            return '启用 SSL 需同时提供证书与私钥路径'
        return ''

    # ── Nginx 配置渲染 ────────────────────────────────────
    def to_ngx_spec(self, log_dir: str = '') -> dict:
        """映射为 ngxconf 机制包的站点描述 —— 渲染不在这里做。

        ngxconf 只认它文档里那套字段（id/name/domains/kind/root_dir/index/
        target/php_socket/ssl/log_dir），所以「映射」是扩展的职责、
        「渲染」是机制包的职责，两者边界清楚。
        """
        return {
            'id': self.id, 'name': self.name, 'domains': self.domains,
            'kind': self.kind, 'root_dir': self.root_dir, 'index': self.index,
            'target': self.target, 'php_socket': self._php_socket(),
            'ssl': {'enabled': self.enable_ssl, 'cert': self.ssl_cert,
                    'key': self.ssl_key},
            'log_dir': log_dir or '',
        }

    def _php_socket(self) -> str:
        """PHP 站点实际使用的 fastcgi 端点。

        - 指定了 ``php_version``：交给 compat 按版本解出约定端点
          （本机叫不出 compat 时，退回静态 php_fastcgi，站点照样能建）。
        - 没指定：沿用用户填的 php_fastcgi（兼容老数据 / 高级自定义）。
        """
        if self.kind != KIND_PHP or not self.php_version:
            return self.php_fastcgi
        try:
            import zkg
            c = zkg.require('compat')
            plat = 'win' if sys.platform.startswith('win') else 'nix'
            return c.php_fastcgi(self.php_version, plat)
        except Exception as e:
            logger.warning("[sites] 按版本解 fastcgi 失败(%s)，退回静态: %s",
                           self.php_version, e)
            return self.php_fastcgi

    def to_vhost_spec(self) -> dict:
        """映射为 vhost 机制包的站点描述（内置虚拟主机的兜底后端用）。"""
        return {
            'id': self.id, 'domains': self.domains, 'kind': self.kind,
            'root_dir': self.root_dir, 'index': self.index, 'target': self.target,
        }

    def render_nginx(self, log_dir: str = '') -> str:
        """生成 server 块（不含 http 上下文）—— 实现全在 ngxconf 机制包。"""
        import zkg
        return zkg.require('ngxconf').render_server(self.to_ngx_spec(log_dir))
    def to_dict(self) -> dict:
        d = self.to_config()
        d['php_socket'] = self._php_socket()
        return d


def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, str):
        parts = [p.strip() for p in v.replace(';', ',').replace('\n', ',').split(',')]
        return [p for p in parts if p]
    if isinstance(v, (list, tuple, set)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v)]


def _now() -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S')
