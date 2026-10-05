"""Zeronus 包管理器（apt 式依赖解析器）。

设计哲学（类 Linux 内核）：
- 内核（framework）本身「啥都不做」，只提供最小运行时。
- 官方能力 = 官方工具包（tool package），默认随发行包 bundled，
  也可从远程「镜像源」（类 apt sources.list）拉取。
- 插件在 manifest 里声明 dependencies，框架启动扫描所有插件后，
  按声明解析需要加载哪些官方工具，未被任何插件依赖的工具直接跳过。
- 依赖图记录在独立的 plugins.db（与运行时/配置 DB 物理分离），每次启动重建。

本子包纯标准库实现，不引入任何第三方依赖。
"""

from .manifest import Manifest
from .sources import Source, SourceRegistry
from .credentials import CredentialStore
from .scanner import scan_dir
from .resolver import Resolver, Resolution
from .depdb import DepDB

__all__ = [
    "Manifest",
    "Source",
    "SourceRegistry",
    "CredentialStore",
    "scan_dir",
    "Resolver",
    "Resolution",
    "DepDB",
]
