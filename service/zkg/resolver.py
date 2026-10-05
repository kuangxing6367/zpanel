"""依赖解析器（类 apt 的 resolver）。

职责：
1. 汇总所有启用源提供的「官方工具包」清单（来自各源 dist/index.json）。
2. 汇总所有插件 manifest（插件在 dependencies 里声明它需要的官方工具）。
3. 计算每个官方工具被哪些插件依赖（dependents）。
4. 标记「声明了但任何源都没有」的依赖为 missing。
5. 输出 Resolution，供 loader 决定加载哪些工具、供 depdb 记录依赖图。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .manifest import Manifest
from .sources import Source, SourceRegistry


@dataclass
class Resolution:
    # tool_id -> {manifest: dict, source_id: str, dependents: List[str]}
    needed: Dict[str, dict] = field(default_factory=dict)
    # 所有源里能找到的工具（含无人依赖的），用于 depdb 建全量表
    all_tools: Dict[str, dict] = field(default_factory=dict)
    # 插件声明了但任何源都没有的工具 id
    missing: List[str] = field(default_factory=list)


class Resolver:
    def __init__(self, registry: SourceRegistry):
        self.registry = registry

    def _collect_tools(self) -> Dict[str, dict]:
        """汇总所有启用源的工具包： tool_id -> {manifest, source_id, source_kind}。

        顺序：官方源优先、社区源其后；同名包官方胜出（社区包不得覆盖官方包）。
        """
        tools: Dict[str, dict] = {}
        ordered = self.registry.official() + self.registry.community()
        for src in ordered:
            try:
                idx = src.fetch_index()
            except Exception as e:
                print(f"[pkg] 源 {src.id} 索引获取失败（跳过）: {e}")
                continue
            for p in idx.get("packages", []):
                # 先到的源不覆盖后到的（官方优先胜出）
                if p["id"] not in tools:
                    tools[p["id"]] = {
                        "manifest": p,
                        "source_id": src.id,
                        "source_kind": src.kind,
                    }
        return tools

    def resolve(self, plugin_manifests: List[Manifest]) -> Resolution:
        tools = self._collect_tools()
        res = Resolution()
        res.all_tools = {tid: v for tid, v in tools.items()}

        dependents: Dict[str, List[str]] = {}
        missing: set = set()

        for pm in plugin_manifests:
            for dep in pm.dependencies:
                if dep in tools:
                    dependents.setdefault(dep, []).append(pm.id)
                else:
                    missing.add(dep)

        for tid, v in tools.items():
            deps = dependents.get(tid, [])
            res.needed[tid] = {
                "manifest": v["manifest"],
                "source_id": v["source_id"],
                "source_kind": v.get("source_kind", "official"),
                "dependents": deps,
            }

        res.missing = sorted(missing)
        return res

    def summary(self, res: Resolution) -> str:
        lines = ["[pkg] 解析结果:"]
        for tid, v in sorted(res.needed.items()):
            n = len(v["dependents"])
            mark = "加载" if n > 0 else "跳过(无依赖)"
            lines.append(f"  - {tid}@{v['manifest']['version']} [{v['source_id']}] -> {mark} (依赖方 {n})")
        if res.missing:
            lines.append("[pkg] 缺失依赖(任何源都没有): " + ", ".join(res.missing))
        return "\n".join(lines)
