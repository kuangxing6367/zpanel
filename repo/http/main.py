"""http —— 统一 HTTP 客户端（机制包）。

框架提供的一等公民程序化接口：插件调外部 API 统一走这里，
不再各自手搓 urllib + try/except + json 解析三板斧。

稳定 API 表面：
    request(method, url, *, params=None, json=None, data=None,
            headers=None, timeout=15.0, max_bytes=4194304,
            proxies=UNSET) -> HttpResult
    get(url, **kw) / post(url, **kw) / put(url, **kw) / delete(url, **kw)
    HttpResult.ok / .status / .headers / .text / .data / .error

设计约定：
- 任何网络异常都不抛——返回 ok=False + .error 字符串，插件判 ok 即可；
- 响应体超 max_bytes 截断为错误（防内存炸）；
- 传 json= 自动序列化并带 Content-Type，响应按 JSON 解析进 .data（失败为 None）；
- 回环地址（127.0.0.1 / localhost / ::1）强制绕过环境代理（走代理访问本机必错），
  其余地址默认跟随环境代理，可用 proxies={} 显式绕过或 proxies={...} 指定。
"""
from __future__ import annotations

import json as _json
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_BYTES = 4 * 1024 * 1024
_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
_OPENER_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_UNSET = object()


class HttpResult:
    """HTTP 结果对象：永不抛异常，判 .ok 即可。"""

    def __init__(self, status: int = 0, headers: dict = None,
                 body: bytes = b"", error: str = ""):
        self.status = status
        self.headers = headers or {}
        self._body = body
        self.error = error

    @property
    def ok(self) -> bool:
        return self.error == "" and 200 <= self.status < 400

    @property
    def text(self) -> str:
        try:
            return self._body.decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            return ""

    @property
    def data(self):
        """响应体按 JSON 解析；失败返回 None（配合 .text 兜底）。"""
        try:
            return _json.loads(self.text)
        except (ValueError, TypeError):
            return None


def request(method: str, url: str, *, params: dict = None,
            json: dict = None, data=None, headers: dict = None,
            timeout: float = DEFAULT_TIMEOUT,
            max_bytes: int = DEFAULT_MAX_BYTES,
            proxies=_UNSET) -> HttpResult:
    """发起 HTTP 请求。json= 优先于 data=；params= 追加到查询串。

    proxies：None=跟随环境代理；{}=绕过所有代理；dict=指定代理。
    回环地址无条件绕过代理。
    """
    if params:
        sep = "&" if urllib.parse.urlparse(url).query else "?"
        url = url + sep + urllib.parse.urlencode(params)
    hdrs = dict(headers or {})
    body = None
    if json is not None:
        body = _json.dumps(json, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json; charset=utf-8")
    elif data is not None:
        if isinstance(data, (dict, list)):
            body = urllib.parse.urlencode(data).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif isinstance(data, str):
            body = data.encode("utf-8")
        else:
            body = data
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method.upper())
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in _LOOPBACK:
        opener = _OPENER_NO_PROXY
    elif proxies is _UNSET:
        opener = urllib.request.build_opener()   # 跟随环境
    else:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(proxies or {}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(max_bytes + 1)
            if len(raw) > max_bytes:
                return HttpResult(error=f"响应超过大小上限 {max_bytes} 字节")
            return HttpResult(resp.status, dict(resp.headers), raw)
    except urllib.error.HTTPError as e:
        return HttpResult(e.code, dict(e.headers or {}), e.read(max_bytes))
    except Exception as e:  # URLError / timeout / 连接错误等
        return HttpResult(error=f"{type(e).__name__}: {e}")


def get(url: str, **kw) -> HttpResult:
    return request("GET", url, **kw)


def post(url: str, **kw) -> HttpResult:
    return request("POST", url, **kw)


def put(url: str, **kw) -> HttpResult:
    return request("PUT", url, **kw)


def delete(url: str, **kw) -> HttpResult:
    return request("DELETE", url, **kw)
