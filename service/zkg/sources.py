"""源（source）抽象 —— 类 apt 的 sources.list。

两种源：
- ``local``：本地仓库目录，结构 ``<path>/dist/index.json``（+ 可选 ``dist/indices/*.json`` 分片）
            + ``<path>/pool/*.tar.gz``。
- ``http`` ：远程镜像源（如 ``https://zkg.zgric.top/zeronus``），同样提供
            ``/dist/index.json`` 与 ``/pool/*.tar.gz``，用标准库 urllib 拉取，无第三方依赖。

官方源与社区源：
- 每个源用 ``kind`` 标记：``official``（官方，默认）/ ``community``（社区）。
- 官方源与社区源必须**分开配置为不同的源**（由官方/发布方负责维护各自的仓库），
  框架只负责按源解析、合并索引；同名包以「官方源优先」胜出。
- 社区包与官方包用同一套 JSON 索引 schema：每个条目既描述「程序」（包元数据），
  也给出「下载地址」(``url``) 与完整性校验 (``sha256``)。

索引分片（自动合并）：
- 一个源可以提供多份 index JSON（包过多时拆分），框架启动时自动合并：
  - 本地：除 ``dist/index.json`` 主文件外，自动扫描 ``dist/indices/*.json`` 与
    ``dist/index-*.json`` 作为分片；主文件也可用 ``"splits": [...]`` 显式列出分片。
  - 远程：主文件 ``dist/index.json`` 通过 ``"splits": [...]`` 列出分片（HTTP 无法列目录），
    框架逐个拉取合并。
- 分片文件各自是 ``{"packages": [...]}``，框架把它们的 packages 合并成一份完整索引。
"""

from __future__ import annotations

import glob
import json
import os
import urllib.request
import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger('zernus.zkg')


@dataclass
class Source:
    id: str
    type: str  # 'local' | 'http'
    url: Optional[str] = None
    path: Optional[str] = None
    enabled: bool = True
    auth: str = "none"
    kind: str = "official"  # 'official' | 'community'

    def _base(self) -> str:
        if self.type == "local":
            return os.path.abspath(self.path or "")
        return self.url.rstrip("/")

    # ── 索引获取（自动合并多份 JSON）──

    def _http_json(self, target: str) -> dict:
        req = urllib.request.Request(target, headers={"User-Agent": "Zeronus-Pkg/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _iter_index_dicts(self):
        """逐个 yield 该源所有索引分片（dict），框架随后合并。"""
        if self.type == "local":
            dist = os.path.join(self._base(), "dist")
            # 主文件
            primary = os.path.join(dist, "index.json")
            files: List[str] = []
            if os.path.isfile(primary):
                files.append(primary)
                with open(primary, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for sp in data.get("splits", []):
                    cand = os.path.join(dist, sp)
                    if os.path.isfile(cand) and cand not in files:
                        files.append(cand)
            # 自动发现其它分片
            for pat in (os.path.join(dist, "indices", "*.json"),
                        os.path.join(dist, "index-*.json")):
                for fp in sorted(glob.glob(pat)):
                    if fp not in files:
                        files.append(fp)
            for fp in files:
                with open(fp, "r", encoding="utf-8") as f:
                    yield json.load(f)
        elif self.type == "http":
            try:
                primary = self._http_json(self._base() + "/dist/index.json")
            except Exception as e:
                logger.warning(f"源 {self.id} 主索引获取失败（跳过）: {e}")
                return
            yield primary
            for sp in primary.get("splits", []):
                try:
                    yield self._http_json(self._base() + "/dist/" + sp.lstrip("/"))
                except Exception as e:
                    logger.warning(f"源 {self.id} 分片 {sp} 获取失败（跳过）: {e}")
        else:
            raise ValueError(f"未知源类型: {self.type}")

    def fetch_index(self) -> dict:
        """返回合并后的仓库索引 ``{"packages": [...]}``（自动合并多份分片）。"""
        merged: dict = {"packages": [], "generated_by": "zeronus-pkg"}
        for part in self._iter_index_dicts():
            for p in part.get("packages", []):
                merged["packages"].append(p)
            # 保留主文件里的其它元信息
            for k, v in part.items():
                if k == "packages":
                    continue
                merged.setdefault(k, v)
        return merged

    def fetch_package(self, url: str, dest_dir: str) -> str:
        """按索引里的 ``url`` 字段把包文件下载/复制到 dest_dir，返回本地落盘路径。

        ``url`` 可以是：
        - 绝对 http(s) 地址（社区包可托管在任意位置）；
        - 相对路径（如 ``pool/xxx.tar.gz``，相对源根，本地/远程都支持）。
        """
        os.makedirs(dest_dir, exist_ok=True)
        # 绝对地址（http(s)/file/... 任意 scheme）直接下载；相对路径按源根解析
        is_abs = "://" in url

        if is_abs:
            target = url
        elif self.type == "local":
            target = os.path.join(self._base(), url)
        else:  # http + 相对
            target = self._base() + "/" + url.lstrip("/")

        fname = os.path.basename(target.split("?")[0]) or "pkg.tar.gz"
        dst = os.path.join(dest_dir, fname)

        if is_abs or self.type == "http":
            req = urllib.request.Request(target, headers={"User-Agent": "Zeronus-Pkg/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp, open(dst, "wb") as fo:
                fo.write(resp.read())
        else:  # local + 相对
            with open(target, "rb") as fi, open(dst, "wb") as fo:
                fo.write(fi.read())
        return dst


class SourceRegistry:
    def __init__(self, sources: Optional[List[Source]] = None):
        self.sources: List[Source] = sources or []

    @classmethod
    def from_config(cls, cfg: Optional[dict]) -> "SourceRegistry":
        """从配置 dict（pkg.sources 列表）构建。"""
        reg = cls()
        raw = (cfg or {}).get("sources", []) if cfg else []
        for s in raw:
            reg.sources.append(
                Source(
                    id=s["id"],
                    type=s["type"],
                    url=s.get("url"),
                    path=s.get("path"),
                    enabled=bool(s.get("enabled", True)),
                    auth=s.get("auth", "none"),
                    kind=s.get("kind", "official"),
                )
            )
        return reg

    def enabled(self) -> List[Source]:
        return [s for s in self.sources if s.enabled]

    def official(self) -> List[Source]:
        """已启用的官方源。"""
        return [s for s in self.sources if s.enabled and s.kind == "official"]

    def community(self) -> List[Source]:
        """已启用的社区源。"""
        return [s for s in self.sources if s.enabled and s.kind == "community"]
