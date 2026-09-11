"""本文件对外提供「消息引用材料」的文本约定，是消息文本与材料标识之间唯一的耦合点。

图片以引用而非内联载荷留在对话状态中（像素只在模型请求层注入），
因此需要有稳定可解析的引用标记，用于前端渲染缩略图、以及压缩豁免的依赖判定。

对外提供:
    MATERIAL_REF_PATTERN — 匹配单个引用标记的正则
    format_material_ref — 生成引用标记
    material_ref_ids — 取出一段文本里引用的全部材料标识
    strip_material_refs — 移除文本中的引用标记
    references_material — 判定文本是否引用了给定材料

输入:
    index: int — 图片在本次消息中的序号（从 1 开始，仅供人读）
    material_id: str — 材料标识
    text: str — 任意消息文本

输出:
    format_material_ref → str；material_ref_ids → set[str]
    strip_material_refs → str；references_material → bool

具体工作流:
    (1) 标记形态为 `【图片N material_id=<id>】`：对人可读、对模型可读、对正则可解析
    (2) material_ref_ids 全量扫描文本，允许一条消息引用多张图片
    (3) 前端 desktop/app.js 内的正则必须与本文件保持一致，两侧改动需同步

示例:
    format_material_ref(1, "ab12") → "【图片1 material_id=ab12】"
    material_ref_ids("看这张【图片1 material_id=ab12】") → {"ab12"}
"""

import re

MATERIAL_REF_PATTERN = re.compile(r"【图片(\d+) material_id=([A-Za-z0-9_-]+)】")


def format_material_ref(index: int, material_id: str) -> str:
    return f"【图片{index} material_id={material_id}】"


def material_ref_ids(text: str) -> set[str]:
    if not isinstance(text, str):
        return set()
    return {match.group(2) for match in MATERIAL_REF_PATTERN.finditer(text)}


def strip_material_refs(text: str) -> str:
    if not isinstance(text, str):
        return ""
    return MATERIAL_REF_PATTERN.sub("", text)


def references_material(text: str, material_id: str) -> bool:
    return material_id in material_ref_ids(text)
