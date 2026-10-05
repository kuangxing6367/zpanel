# -*- coding: utf-8 -*-
"""内核 API 路由集（core/api/routes）

每个子模块暴露 `register(app, ctx)`，由 `core/api/app.py` 统一装配。
`ctx` 为 `ApiContext`，提供统一响应封装与鉴权装饰器。
"""


def register_all(app, ctx):
    from . import auth, system, nodes
    auth.register(app, ctx)
    system.register(app, ctx)
    nodes.register(app, ctx)
