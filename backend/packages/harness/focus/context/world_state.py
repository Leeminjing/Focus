"""本文件对外提供 WorldSection 与 checkpoint WorldState 的纯 full/diff 编译器。

输入为当前稳定 sections、精确 checkpoint 基线和仍保留的消息身份；输出为 append-only 更新消息与新 snapshot。
具体工作流为校验 renderer、binding 和完整依赖链，未知基线完整重建，权限完整替换，catalog 给出新增/移除/改写。
示例：updates, snapshot = compile_world_state(sections, previous, retained_ids, binding)。
"""

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any, Literal

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from focus.history import content_hash


RENDERER_VERSION = "focus-world-state-v1"


@dataclass(frozen=True)
class WorldSection:
    section_id: str
    authority: Literal["policy", "runtime_fact", "capability"]
    data: dict[str, Any]
    strategy: Literal["replacement", "catalog"] = "replacement"


def compile_world_state(
    sections: tuple[WorldSection, ...], previous: dict | None,
    retained_ids: set[str], binding: dict[str, Any],
) -> tuple[list[BaseMessage], dict]:
    if len({section.section_id for section in sections}) != len(sections):
        raise ValueError("WorldState section ID 重复")
    known = bool(previous and previous.get("renderer_version") == RENDERER_VERSION
                 and previous.get("binding") == binding and previous.get("anchor_id") in retained_ids)
    old = previous.get("sections", {}) if known else {}
    snapshot = {"renderer_version": RENDERER_VERSION, "binding": deepcopy(binding), "sections": {}}
    updates: list[BaseMessage] = []
    anchor_id = previous["anchor_id"] if known else "world:" + content_hash([
        "anchor", binding, RENDERER_VERSION, sorted(section.section_id for section in sections), content_hash(previous or {}),
    ])
    snapshot["anchor_id"] = anchor_id
    if not known:
        updates.append(SystemMessage(
            id=anchor_id,
            content="WorldState baseline reset: all earlier WorldState sections no longer apply. "
                    "The following current sections establish the new baseline; later section updates replace or extend it: "
                    + ", ".join(sorted(section.section_id for section in sections)),
            additional_kwargs={"focus_context": {"origin": "runtime", "scope": "runtime", "kind": "world_state_update"}},
        ))
    for section in sections:
        baseline = old.get(section.section_id)
        anchored = bool(baseline and baseline.get("retained_refs")
                        and "data" in baseline
                        and set(baseline["retained_refs"]).issubset(retained_ids)
                        and baseline.get("authority") == section.authority
                        and baseline.get("strategy") == section.strategy
                        and baseline.get("hash") == content_hash([baseline["authority"], baseline["strategy"], baseline["data"]]))
        current_hash = content_hash([section.authority, section.strategy, section.data])
        if anchored and baseline.get("hash") == current_hash:
            snapshot["sections"][section.section_id] = deepcopy(baseline)
            continue
        mode = "full" if not anchored else "replacement"
        body = deepcopy(section.data)
        refs = list(baseline["retained_refs"]) if anchored else []
        if anchored and section.strategy == "catalog":
            mode, body = "diff", _catalog_diff(baseline["data"], section.data)
        anchor = refs if anchored else content_hash(previous or {})
        update_id = "world:" + content_hash([binding, RENDERER_VERSION, section.section_id, current_hash, anchor])
        message_type = HumanMessage if section.authority == "runtime_fact" else SystemMessage
        updates.append(message_type(
            id=update_id,
            content=f'<focus_world_state section="{section.section_id}" update="{mode}">\n'
                    + json.dumps(body, ensure_ascii=False, sort_keys=True) + "\n</focus_world_state>",
            additional_kwargs={"focus_context": {
                "origin": "runtime", "scope": "runtime", "kind": "world_state_update",
                "authority": section.authority, "section_id": section.section_id,
                "source_refs": [{"renderer_version": RENDERER_VERSION, "state_hash": current_hash}],
            }},
        ))
        snapshot["sections"][section.section_id] = {
            "hash": current_hash, "data": deepcopy(section.data), "authority": section.authority, "strategy": section.strategy,
            "retained_refs": [*refs, update_id] if mode == "diff" else [anchor_id, update_id],
        }
    current_ids = {section.section_id for section in sections}
    for section_id in old.keys() - current_ids:
        baseline = old[section_id]
        if not set(baseline.get("retained_refs", ())).issubset(retained_ids):
            continue
        identity = "world:" + content_hash([binding, RENDERER_VERSION, section_id, "removed", baseline["hash"]])
        message_type = HumanMessage if baseline["authority"] == "runtime_fact" else SystemMessage
        updates.append(message_type(id=identity, content=f'WorldState section "{section_id}" removed; previous contents no longer apply.',
                                    additional_kwargs={"focus_context": {"origin": "runtime", "scope": "runtime", "kind": "world_state_update"}}))
    return updates, snapshot


def _catalog_diff(previous: dict, current: dict) -> dict:
    return {
        "added": {key: current[key] for key in current.keys() - previous.keys()},
        "removed": sorted(previous.keys() - current.keys()),
        "changed": {key: current[key] for key in current.keys() & previous.keys() if current[key] != previous[key]},
    }
