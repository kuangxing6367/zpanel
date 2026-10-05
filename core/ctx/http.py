# -*- coding: utf-8 -*-
"""HTTP 客户端（core/ctx 节点 15）

为插件提供开箱即用的 http_get/http_post（同步）与 http_get_async/
http_post_async（异步），免去插件自引 requests/aiohttp 的依赖负担。
基于标准库 urllib 实现，内核零第三方依赖；异步版通过事件循环线程池
执行，不阻塞消息处理。
"""
import asyncio
import json as _json
import urllib.parse
import urllib.request

import logging

_logger = logging.getLogger('zernus')


class HttpMixin:
    """ctx 上的 HTTP 请求能力。"""

    def http_get(self, url: str, params: dict = None, headers: dict = None,
                 timeout: float = 10, **kwargs) -> tuple:
        """同步 GET，返回 (status_code, body_text)。"""
        if params:
            url = url + ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
        return self._request('GET', url, headers=headers, timeout=timeout, **kwargs)

    def http_post(self, url: str, data=None, json_body=None, headers: dict = None,
                  timeout: float = 10) -> tuple:
        """同步 POST。data 为表单/字节，json_body 为 dict（自动序列化+设 Content-Type）。"""
        body = None
        hdrs = dict(headers or {})
        if json_body is not None:
            body = _json.dumps(json_body).encode('utf-8')
            hdrs.setdefault('Content-Type', 'application/json')
        elif data is not None:
            if isinstance(data, (dict, list)):
                body = urllib.parse.urlencode(data).encode('utf-8')
                hdrs.setdefault('Content-Type', 'application/x-www-form-urlencoded')
            elif isinstance(data, (str, bytes)):
                body = data.encode('utf-8') if isinstance(data, str) else data
        return self._request('POST', url, body=body, headers=hdrs, timeout=timeout)

    async def http_get_async(self, url: str, params: dict = None, headers: dict = None,
                             timeout: float = 10) -> tuple:
        """异步 GET（线程池执行 urllib，不阻塞事件循环）。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self.http_get(url, params=params, headers=headers, timeout=timeout)
        )

    async def http_post_async(self, url: str, data=None, json_body=None, headers: dict = None,
                              timeout: float = 10) -> tuple:
        """异步 POST（线程池执行 urllib，不阻塞事件循环）。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self.http_post(url, data=data, json_body=json_body,
                                         headers=headers, timeout=timeout)
        )

    def _request(self, method: str, url: str, body=None, headers=None, timeout: float = 10):
        req = urllib.request.Request(url, data=body, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode('utf-8', errors='replace')
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode('utf-8', errors='replace')
        except Exception as e:
            _logger.warning(f"[{getattr(self, '_plugin_name', '?')}] HTTP {method} {url} 失败: {e}")
            raise
