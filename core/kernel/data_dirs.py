"""数据目录迁移（内核细模块，纯 stdlib）。

把旧版散落在项目根的 logs/、plugins_dat/ 一次性迁移到 data/ 下（仅当目标不存在）。
"""

from __future__ import annotations

import logging
import os
import shutil

logger = logging.getLogger("zernus")


def migrate_legacy_data_dirs(project_root: str) -> None:
    data_dir = os.path.join(project_root, "data")
    os.makedirs(data_dir, exist_ok=True)
    for old_name, new_name in (("logs", "logs"), ("plugins_dat", "plugins_dat")):
        old_path = os.path.join(project_root, old_name)
        new_path = os.path.join(data_dir, new_name)
        if os.path.isdir(old_path) and not os.path.exists(new_path):
            try:
                shutil.move(old_path, new_path)
                logger.info(f"数据目录迁移: {old_path} → {new_path}")
            except Exception as e:
                logger.warning(f"数据目录迁移失败 [{old_name}]: {e}")
