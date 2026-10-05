"""加载器（依赖驱动加载）。

做法：
    scan(plugins) -> resolve(依赖图) -> rebuild(plugins.db)
    -> 仅加载「有依赖方」的官方工具

每个官方工具在 plugins.db 里对应一张 pkg_<T> 表（见 depdb），
零行 = 无依赖 = 不加载。
"""
from __future__ import annotations

import importlib.util
import logging
import os
import tarfile
from typing import List, Optional

from . import defaults, scanner, resolver, depdb, sources
from . import api as zkg_api

logger = logging.getLogger('zernus.zkg')


class Loader:
    def __init__(self, plugins_dir: str, data_dir: str,
                 sources_cfg: Optional[list] = None,
                 scan_roots: Optional[List[str]] = None,
                 plugin_api_version: Optional[int] = None):
        """
        :param plugins_dir: 主扫描根（生产为 repo/，含官方工具包）
        :param scan_roots:  额外扫描根列表（如 software/plugins/，用户插件在
                            manifest.toml 的 dependencies 里声明所需工具）；
                            为 None 时仅扫描 plugins_dir（兼容旧行为）。
                            同 id 的包以先扫到的为准（主扫描根优先）。
        :param plugin_api_version: 当前框架插件 API 版本（core.ctx.PLUGIN_API_VERSION）；
                            提供后对声明了 api_version 的插件清单做兼容校验并记录结果。
        """
        self.plugins_dir = plugins_dir
        self.scan_roots = scan_roots
        self.data_dir = data_dir
        self._api = plugin_api_version
        cfg = {"sources": sources_cfg} if sources_cfg else \
              {"sources": defaults.get_default_sources()}
        self.registry = sources.SourceRegistry.from_config(cfg)
        self.resolver = resolver.Resolver(self.registry)
        self.depdb = depdb.DepDB(os.path.join(data_dir, "plugins.db"))
        self._loaded: dict = {}

    def run(self) -> dict:
        # 机制包之间经顶层 `zkg` 取用彼此（probe -> procs 之类）。
        # 必须在任何包被 import 之前注册，否则包内 `import zkg` 会失败。
        zkg_api.register()
        roots = [self.plugins_dir] + list(self.scan_roots or [])
        plugin_manifests: List = []
        seen: set = set()
        for root in roots:
            for m in scanner.scan_dir(root):
                if m.id in seen:      # 主扫描根优先，同 id 去重
                    continue
                seen.add(m.id)
                plugin_manifests.append(m)
        # 插件 API 兼容校验（声明了 api_version 且不兼容的，记录并告警）
        api_incompatible: List[str] = []
        if self._api is not None:
            for m in plugin_manifests:
                if not m.api_version_ok(self._api):
                    api_incompatible.append(m.id)
                    logger.warning(
                        f"[loader] 插件 {m.id} 声明 api_version={m.api_version!r}，"
                        f"与当前插件 API 版本 {self._api} 不兼容，"
                        f"可能运行异常（请升级插件或调整声明）"
                    )
        res = self.resolver.resolve(plugin_manifests)
        stats = self.depdb.rebuild(plugin_manifests, res)
        for tid in res.needed:
            if self.depdb.is_loaded(tid):
                self._load_tool(tid, res.needed[tid])
        return {
            "stats": stats,
            "plugin_count": len(plugin_manifests),
            "loaded_tools": sorted(self._loaded.keys()),
            "skipped_tools": sorted(t for t in res.needed
                                    if not self.depdb.is_loaded(t)),
            "missing": list(res.missing),
            "api_incompatible": api_incompatible,
        }

    def _load_order(self, needed: dict) -> list:
        """机制包加载顺序：先依赖、后使用者（同层保持字母序，结果可复现）。

        只考虑「机制包 → 机制包」的边；插件（软件层）依赖不参与排序，
        因为插件不由 zkg 加载（由 core.runtime 装载），它们只需要包已就绪。
        检测到环时打日志并忽略该边，绝不因为坏依赖导致启动卡死。
        """
        order: list = []
        state: dict = {}

        def visit(tid: str, stack: list):
            st = state.get(tid)
            if st == 2:
                return
            if st == 1:
                logger.warning(
                    f"[loader] 机制包依赖成环，忽略此边: {' -> '.join(stack + [tid])}")
                return
            state[tid] = 1
            deps = ((needed.get(tid) or {}).get("manifest") or {}).get("dependencies") or []
            for dep in sorted(deps):
                if dep in needed and dep != tid:
                    visit(dep, stack + [tid])
            state[tid] = 2
            order.append(tid)

        for tid in sorted(needed):
            visit(tid, [])
        return order

    def _load_tool(self, tid: str, v: dict):
        mod = self._ensure_and_import(tid, v)
        if mod is not None:
            self._loaded[tid] = mod

    def _ensure_and_import(self, tid: str, v: dict) -> Optional[object]:
        mani = v["manifest"]
        entry = mani.get("entry") or "main.py"
        # 1) 优先本地 repo 目录（开发态，免下载）
        local_pkg = os.path.join(defaults.PROJECT_ROOT, "repo", tid)
        if os.path.isdir(local_pkg):
            return self._import_file(f"{tid}_tool",
                                      os.path.join(local_pkg, entry))
        # 2) 否则从源拉取（本地 pool / 远程下载；url 可为相对或绝对 http(s) 地址）
        cache_dir = os.path.join(self.data_dir, "pkg_cache", tid)
        src = next((s for s in self.registry.enabled()
                    if s.id == v["source_id"]), None)
        if src is None:
            print(f"[loader] 找不到提供 {tid} 的源，跳过")
            return None
        local_file = src.fetch_package(mani.get("url", f"{tid}.tar.gz"), cache_dir)
        # 完整性校验：索引声明的 sha256 必须与落盘包体一致，否则拒绝加载
        expected = str(mani.get("sha256") or "").strip().lower()
        if expected:
            actual = self._sha256_file(local_file)
            if actual != expected:
                logger.error(
                    f"[loader] 包 {tid} sha256 校验失败（期望 {expected[:16]}…，"
                    f"实际 {actual[:16]}…），已拒绝加载。包体可能被篡改或损坏。"
                )
                return None
        # 签名校验（可选）：索引条目带 signature 时，加载端必须提供同一密钥
        # （环境变量 ZKG_SIGNING_KEY）。带签名而本地无密钥 → 拒绝（fail closed）。
        sig = str(mani.get("signature") or "").strip().lower()
        if sig:
            key = os.environ.get("ZKG_SIGNING_KEY", "")
            if not key:
                logger.error(
                    f"[loader] 包 {tid} 源声明了 HMAC 签名，但本机未设置 "
                    f"ZKG_SIGNING_KEY，无法验证，已拒绝加载。")
                return None
            if self._hmac_file(key.encode("utf-8"), local_file) != sig:
                logger.error(
                    f"[loader] 包 {tid} 签名校验失败（密钥不匹配或包体被篡改），"
                    f"已拒绝加载。")
                return None
        extract = os.path.join(cache_dir, "extracted")
        os.makedirs(extract, exist_ok=True)
        with tarfile.open(local_file) as tf:
            tf.extractall(extract)
        for root, _d, files in os.walk(extract):
            if entry in files:
                return self._import_file(f"{tid}_tool",
                                          os.path.join(root, entry))
        return None

    @staticmethod
    def _sha256_file(path: str) -> str:
        import hashlib
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _hmac_file(key: bytes, path: str) -> str:
        import hashlib
        import hmac
        h = hmac.new(key, digestmod=hashlib.sha256)
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _import_file(modname: str, path: str):
        import sys
        spec = importlib.util.spec_from_file_location(modname, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[modname] = mod  # 必须先注册，否则 @dataclass 取模块字典失败
        spec.loader.exec_module(mod)
        return mod

    def loaded_tools(self) -> dict:
        return self._loaded
