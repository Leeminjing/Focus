"""本文件对外提供 MaterialContextProjection 与 MaterialContextProjector。

输入为不可变 RunMaterialInputs 和工作区路径；输出为仅含本轮已选材料的策略文本与
<current_uploads> 标签。具体工作流为按绑定顺序解析安全路径，为图片或普通材料生成长期策略
描述；带 snapshot_object_id 的受保护内容明确指向 Git blob，并完全忽略未选材料；projector
不查询数据库也不修改运行聚合。

示例：projection = MaterialContextProjector.project(inputs, workspace_path)。
"""

from dataclasses import dataclass

from backend.app.desktop.material_files import resolve_material_path
from focus.agents.material_inputs import RunMaterialInput, RunMaterialInputs


@dataclass(frozen=True, slots=True)
class MaterialContextProjection:
    policy_text: str
    uploads_tag: str


class MaterialContextProjector:
    @classmethod
    def project(cls, inputs: RunMaterialInputs, workspace_path: str) -> MaterialContextProjection:
        policy = "\n".join(cls._policy_line(item, workspace_path) for item in inputs.attached)
        uploads = [item.relative_path for item in inputs.attached]
        uploads_tag = (
            "<current_uploads>\n" + "\n".join(uploads) + "\n</current_uploads>"
            if uploads else ""
        )
        return MaterialContextProjection(policy, uploads_tag)

    @staticmethod
    def _policy_line(item: RunMaterialInput, workspace_path: str) -> str:
        path = resolve_material_path(workspace_path, item.relative_path)
        snapshot = (
            f" | 当前路径已变化，必须用 git cat-file blob {item.snapshot_object_id} 读取发送时快照"
            if item.snapshot_object_id else ""
        )
        if item.material_kind == "image":
            return f"- {path} | 图片材料{snapshot}"
        reading = "优先完整阅读" if item.reading_mode == "full" else "优先粗略阅读，需要时仍可完整读取"
        instruction = "严格遵守" if item.instruction_mode == "strict" else "仅供参考"
        return f"- {path} | {reading} | {instruction}{snapshot}"
