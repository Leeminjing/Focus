"""桌面领域共享的值对象、约束或纯函数。"""

from __future__ import annotations

from focus.agents.must_view import report_must_view_images


def _must_view_prompt(materials: list[dict[str, str]]) -> str:
    lines = "\n".join(
        (
            f"- {item['relative_path']} (material_id={item['material_id']})"
            for item in materials
        )
    )
    return f"\n\n<focus_must_view>\n用户把以下图片标记为本轮必须查看。请逐张查看，并调用 {report_must_view_images.name} 为每一张各给一条声明：成功读到该图片的内容即把 read 置为真。\n{lines}\n</focus_must_view>"
