"""桌面领域共享的值对象、约束或纯函数。"""

from __future__ import annotations

from pathlib import Path

from focus.agents.must_view import report_must_view_images
from focus.images import is_image_name


def _must_view_prompt(materials: list[dict[str, str]]) -> str:
    lines = "\n".join(
        (
            f"- {item['relative_path']} (material_id={item['material_id']})"
            for item in materials
        )
    )
    return f"\n\n<focus_must_view>\n用户把以下图片标记为本轮必须查看。请逐张查看，并调用 {report_must_view_images.name} 为每一张各给一条声明：成功读到该图片的内容即把 read 置为真。\n{lines}\n</focus_must_view>"


def _material_policy_line(path: Path, reading_mode: str, instruction_mode: str) -> str:
    if is_image_name(path.name):
        return f"- {path} | 图片材料"
    reading = (
        "优先完整阅读" if reading_mode == "full" else "优先粗略阅读，需要时仍可完整读取"
    )
    instruction = "严格遵守" if instruction_mode == "strict" else "仅供参考"
    return f"- {path} | {reading} | {instruction}"
