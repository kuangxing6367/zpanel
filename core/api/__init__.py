# -*- coding: utf-8 -*-
"""
内核自带 Web API（core/api）

zpanel 把「Web 界面」从扩展提升为**内核能力**：内核直接构建 Flask 应用、
直接托管前端构建产物、直接提供鉴权与核心数据接口。

    core/            内核（sqlite + flask，仅此两样外部能力）
    └── api/         Web API：鉴权 / 路由 / 前端托管
    service/         服务层：zkg 包管理 / 安全传输 / 系统适配
    software/        软件级：功能模块（运行时管理、站点托管……）

对上只暴露三个东西：

    create_api_app(fw) -> Flask    构建应用（含路由与前端托管）
    ApiServer(fw)                  在独立线程中运行的 Web 服务器
    ApiContext                     路由注册上下文（供各层挂接口）

各层挂接口的方式（在扩展 / 模块的 register 里）：

    ctx.register_api('/api/zpanel/runtimes', handler, methods=['GET'])
"""
from .app import create_api_app, ApiServer, ApiContext
from .security import AuthService, hash_password, verify_password

__all__ = [
    'create_api_app', 'ApiServer', 'ApiContext',
    'AuthService', 'hash_password', 'verify_password',
]
