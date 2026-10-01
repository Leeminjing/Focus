"""本文件对外提供 desktop_contexts 与 skill_catalog_records 装配投影。

输入为 Run 已持久化装备、材料读取规则和当前能力目录；输出为 Harness FrozenContext 与轻量 catalog。
具体工作流为核验记忆冻结记录、绑定 Skill 正文内容哈希，将材料读取规则独立为执行 policy。
示例：contexts = desktop_contexts(run.equipment, material_policy)。本模块不读取数据库或磁盘。
"""

import json

from focus.context.scoped import FrozenContext
from focus.history import content_hash


def desktop_contexts(equipment: dict, material_policy: str) -> tuple[FrozenContext, ...]:
    result = [FrozenContext.from_record(raw) for raw in equipment.get("memory_snapshots", ())]
    focus = equipment.get("spatial_focus")
    required = ("spatial_id", "content_ref", "page", "x", "y", "kind", "status")
    if focus and all(key in focus for key in required):
        selected = {key: focus[key] for key in required}
        result.append(FrozenContext(name="spatial_focus", content="<current_spatial_focus>\n" +
            json.dumps(selected, ensure_ascii=False) + "\n用户明确选择了此空间锚点，‘这里’等指代应围绕该坐标解释。\n</current_spatial_focus>",
            source_refs=({"kind": "spatial_focus", "content_hash": content_hash(selected)},)))
    for snapshot in equipment.get("skill_snapshots", ()):
        digest = content_hash(snapshot["content"])
        result.append(FrozenContext(name=f"skill:{snapshot['name']}",
                                   content=f"Selected skill: {snapshot['name']}\n{snapshot['content']}",
                                   source_refs=({"kind": "skill", "name": snapshot["name"], "content_hash": digest},)))
    if material_policy:
        result.append(FrozenContext(name="material_policy", content=material_policy, authority="policy"))
    return tuple(result)


def skill_catalog_records(catalog: dict) -> dict:
    return {name: {"name": skill.name, "description": skill.description,
                   "content_hash": content_hash(skill.content)} for name, skill in sorted(catalog.items())}
