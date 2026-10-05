# -*- coding: utf-8 -*-
"""
前端托管（core/api · 内核出 webui）

内核直接托管前端构建产物，不再依赖任何扩展的「接管」机制：

    frontend/dist/index.html     入口
    frontend/dist/assets/*       打包后的 JS / CSS

路由契约：
- 前缀 `/api/` 之外的 GET 请求一律走静态资源；
- 命中真实文件则原样返回（带缓存头）；
- 未命中则回退 `index.html`（**SPA history 路由**所需：刷新 / 深链接不 404）。

前端目录由 `config.api.frontend_dir` 指定（支持绝对路径或相对项目根），
默认 `frontend/dist`。目录缺失时返回一张内置的说明页，而不是 500——
这样后端可以独立启动、前端可以后补构建。
"""
import logging
import os

from flask import send_from_directory, Response

logger = logging.getLogger('zernus')

_NO_FRONTEND_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>ZPanel · 前端未构建</title>
<style>
 body{background:#0f1115;color:#d8dee9;font:14px/1.7 ui-sans-serif,system-ui,"Microsoft YaHei";
      margin:0;padding:48px;max-width:760px}
 h1{font-size:17px;font-weight:600;letter-spacing:.04em;margin:0 0 6px}
 code{background:#171a21;border:1px solid #232833;border-radius:4px;padding:1px 6px;
      color:#9ecbff;font-size:13px}
 pre{background:#171a21;border:1px solid #232833;border-radius:8px;padding:14px 16px;
     overflow:auto;font-size:12.5px;color:#a6b0bf}
 .k{color:#6b7480;font-size:12px}
</style></head><body>
<h1>前端产物不存在</h1>
<div class="k">内核已就绪，但没有找到前端构建目录。</div>
<pre>%s</pre>
<div class="k">构建后刷新本页即可：</div>
<pre>cd frontend
npm install
npm run build</pre>
<div class="k">也可以先把 <code>config.yaml → api.frontend_dir</code> 指向已有产物目录。</div>
</body></html>"""

# 静态资源的 MIME（Flask 自带 mimetypes 已覆盖绝大多数，这里只补漏）
_EXTRA_MIME = {
    '.js': 'application/javascript; charset=utf-8',
    '.mjs': 'application/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.woff2': 'font/woff2',
    '.json': 'application/json; charset=utf-8',
}


def resolve_frontend_dir(fw, project_root: str) -> str:
    """解析前端目录（config.api.frontend_dir → 默认 frontend/dist）。"""
    cfg = (fw.config.get('api') or {}) if isinstance(fw.config, dict) else {}
    raw = str(cfg.get('frontend_dir') or 'frontend/dist').strip()
    if not raw:
        raw = 'frontend/dist'
    path = raw if os.path.isabs(raw) else os.path.join(project_root, raw)
    return os.path.normpath(path)


def register_static(app, fw, project_root: str):
    """把前端静态资源与 SPA 回退挂到 app 上。"""
    frontend_dir = resolve_frontend_dir(fw, project_root)
    index_file = os.path.join(frontend_dir, 'index.html')

    if not os.path.isdir(frontend_dir):
        logger.warning("[api] 前端目录不存在: %s（将只提供 API）", frontend_dir)
    elif not os.path.isfile(index_file):
        logger.warning("[api] 前端目录缺少 index.html: %s", frontend_dir)

    def _no_frontend():
        body = _NO_FRONTEND_PAGE % f"前端目录: {frontend_dir}"
        return Response(body, status=200, mimetype='text/html')

    def _serve_index():
        if os.path.isfile(index_file):
            resp = send_from_directory(frontend_dir, 'index.html')
            resp.headers['Cache-Control'] = 'no-cache'
            return resp
        return _no_frontend()

    @app.route('/', methods=['GET'])
    def _api_root():
        return _serve_index()

    @app.route('/<path:path>', methods=['GET'])
    def _api_spa(path):
        # API 前缀交给已注册的 API 路由；走到这里说明该接口不存在
        if path.startswith('api/') or path.startswith('healthz'):
            return Response('{"ok": false, "error": "not found"}', status=404,
                            mimetype='application/json')
        candidate = os.path.normpath(os.path.join(frontend_dir, path))
        # 防目录穿越
        if candidate.startswith(frontend_dir) and os.path.isfile(candidate):
            resp = send_from_directory(frontend_dir, path)
            ext = os.path.splitext(path)[1].lower()
            if ext in _EXTRA_MIME:
                resp.headers['Content-Type'] = _EXTRA_MIME[ext]
            if '/assets/' in '/' + path:
                resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
            return resp
        return _serve_index()      # SPA history 回退

    logger.info("[api] 前端托管目录: %s", frontend_dir)
    return frontend_dir
