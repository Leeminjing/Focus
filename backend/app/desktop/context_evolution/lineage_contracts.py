"""本文件对外提供不可变 LineageSnapshot 的完整祖先图合同。

输入为已提交根、Revision 节点及跨 Context 边；输出为身份和拓扑 hash 一致的强类型快照。
具体工作流为验证根与边引用的节点存在且 Context 身份一致，同 Context 边不能冒充派生。
示例：LineageSnapshot.model_validate(reader_snapshot)。合同不提供发布或授权能力。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from backend.app.desktop.domain_evidence.identity import canonical_hash


class LineageNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    revision_id: str
    context_id: str
    generation: int
    redacted: bool = False


class LineageEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_context_id: str
    source_revision_id: str
    target_context_id: str
    target_revision_id: str


class LineageSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    complete: Literal[True] = True
    roots: dict[str, str]
    nodes: tuple[LineageNode, ...]
    edges: tuple[LineageEdge, ...]
    topology_hash: str

    @model_validator(mode="after")
    def _require_complete_graph(self):
        nodes = {node.revision_id: node for node in self.nodes}
        if len(nodes) != len(self.nodes):
            raise ValueError("Lineage 节点重复")
        for context_id, revision_id in self.roots.items():
            if revision_id not in nodes or nodes[revision_id].context_id != context_id:
                raise ValueError("Lineage 根身份缺失或不匹配")
        for edge in self.edges:
            if edge.source_context_id == edge.target_context_id:
                raise ValueError("同 Context 来源不是派生边")
            for revision_id, context_id in (
                (edge.source_revision_id, edge.source_context_id),
                (edge.target_revision_id, edge.target_context_id),
            ):
                if (
                    revision_id not in nodes
                    or nodes[revision_id].context_id != context_id
                ):
                    raise ValueError("Lineage 边身份缺失或不匹配")
        edges = [edge.model_dump(mode="json") for edge in self.edges]
        ordered = sorted(
            edges,
            key=lambda item: (item["target_revision_id"], item["source_revision_id"]),
        )
        if canonical_hash(ordered) != self.topology_hash:
            raise ValueError("Lineage 拓扑 hash 不一致")
        return self
