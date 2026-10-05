"""
扩展点系统（core.hooks）

原 framework/hooks.py（144 行，扩展点契约）已迁至 core/hooks。
framework/hooks.py 仅做透明重导出，调用方零改动。

对外公开：
  - HookPoints   标准扩展点常量
  - HookRegistry 扩展点注册表
"""
from .registry import HookPoints, HookRegistry

__all__ = ['HookPoints', 'HookRegistry']
