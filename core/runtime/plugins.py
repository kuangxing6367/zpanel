"""官方扩展装载节点（software/extensions/ 目录）。

- 装载后调用扩展 ``register(ctx)``，并把模块登记进 plugin_loader。
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys

from core.kernel.paths import project_root

logger = logging.getLogger("zernus")


def load_extensions(fw):
    """加载官方扩展（software/extensions/ 目录）"""
    extensions_dir = os.path.join(project_root(), "software", "extensions")
    if not os.path.isdir(extensions_dir):
        logger.warning(f"extensions 目录不存在: {extensions_dir}")
        return

    # 向后兼容：旧配置键 core_plugins 仍可被识别（新键 extensions 优先）
    core_cfg = fw.config.get("extensions", fw.config.get("core_plugins", {}))

    for name in os.listdir(extensions_dir):
        if name.startswith("_"):
            continue
        plugin_dir = os.path.join(extensions_dir, name)
        main_file = os.path.join(plugin_dir, "main.py")
        if not os.path.isfile(main_file):
            continue

        enabled = core_cfg.get(name, True)
        if enabled is False:
            logger.info(f"官方扩展 [{name}] 已禁用 (extensions.{name}: false)")
            continue

        try:
            spec = importlib.util.spec_from_file_location(f"core_plugin_{name}", main_file)
            module = importlib.util.module_from_spec(spec)
            sys.modules[f"core_plugin_{name}"] = module
            spec.loader.exec_module(module)

            from core.ctx import PluginContext
            ctx = PluginContext(f"core:{name}", fw)
            module.ctx = ctx

            if hasattr(module, "register"):
                module.register(ctx)
                logger.info(f"官方扩展 [{name}] 已加载")
                fw._loaded_extensions.append(name)

                with fw.plugin_loader._lock:
                    meta = getattr(module, "__plugin_meta__", {})
                    fw.plugin_loader._loaded_plugins[name] = {
                        "module": module,
                        "path": plugin_dir,
                        "meta": meta,
                        "priority": meta.get("priority", 50),
                        "yaml": {},
                    }
            else:
                logger.warning(f"官方扩展 [{name}] 无 register 函数")
        except Exception as e:
            logger.error(f"官方扩展 [{name}] 加载失败: {e}", exc_info=True)
