"""
插件上下文对象 (ctx) — core/ctx 公共入口

向插件暴露：命令注册、API调用、数据库、日志、配置读取、事件、定时任务。

原 framework/ctx.py 的 PluginContext 神对象已按单一职责拆为以下细模块，
并在 context.py 中通过多重继承组装回统一的 PluginContext 类（实例接口不变）：

- base          共享状态 + 基础属性
- logger        插件日志适配器 + 异步线程池
- registration  命令/任务/事件注册
- permission    权限组查询（委托 core.perm）
- config        插件配置读取
- api           自定义 API 路由 + 协议 API 调用
- messaging     群动作快捷方法
- roles         身份判定 + 群级插件开关
- database      数据库连接池操作
- audit         插件操作审计
- webui         WebUI 注册 + 注册期集合访问器
- session       多轮会话

framework/ctx.py 仅做 `from core.ctx.context import PluginContext` 兼容重导出。
"""

from .context import PluginContext
from .logger import PluginLogger, _async_executor

# 插件 API 版本（稳定 ABI 承诺，设计总纲第 4 条）。
# 语义：破坏性变更（删方法 / 改签名 / 改行为）→ 主版本 +1；
#       向后兼容的新增 → 保持主版本不变。插件在 manifest.toml 的
#       api_version 字段声明兼容区间（如 "1" 或 ">=1,<2"），zkg 加载器校验。
PLUGIN_API_VERSION = 1

__all__ = ['PluginContext', 'PluginLogger', '_async_executor', 'PLUGIN_API_VERSION']
