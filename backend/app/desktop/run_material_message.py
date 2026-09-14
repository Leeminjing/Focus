"""本文件对外提供 RunMaterialMessageProjector，把正文与逐材料备注组合为用户消息。

输入为字符串或内容块正文以及带稳定 origin_message_id 的 RunMaterialInputs；输出为 LangChain
兼容的 human 消息字典。具体工作流为保留正文原结构，在同一 user role 中追加确定性协议块，
其中逐项携带身份、快照路径、类型、备注和必看意图；空材料只增加稳定消息 ID。

示例：message = RunMaterialMessageProjector.project("比较", inputs)。
"""

import json
from typing import Any

from focus.agents.material_inputs import RunMaterialInputs


class RunMaterialMessageProjector:
    @classmethod
    def project(
        cls,
        message: str | list[dict[str, Any]],
        inputs: RunMaterialInputs,
    ) -> dict[str, Any]:
        content: str | list[dict[str, Any]] = message
        block = cls._material_block(inputs)
        if block:
            if isinstance(message, str):
                content = f"{message}\n\n{block}"
            else:
                content = [*message, {"type": "text", "text": block}]
        return {"role": "human", "content": content, "id": inputs.origin_message_id}

    @staticmethod
    def _material_block(inputs: RunMaterialInputs) -> str:
        if not inputs.attached:
            return ""
        required = set(inputs.required_image_ids)
        payload = [
            {
                "material_id": item.material_id,
                "relative_path": item.relative_path,
                "material_kind": item.material_kind,
                "note": item.note,
                "must_view": item.material_id in required,
            }
            for item in inputs.attached
        ]
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        encoded = encoded.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
        return (
            "<focus_run_materials>\n"
            + encoded
            + "\n</focus_run_materials>"
        )
