"""本文件对外提供材料文件在工作区内的落位与读取装配，是附件目录约定的唯一归属地。

对外提供:
    MATERIAL_ATTACHMENTS_SUBDIR — 材料附件在工作区内的专用子目录（相对路径）
    EmptyUpload / OversizedImage — 落盘前把关的两类拒绝原因
    guard_upload — 落盘前把关：空内容与超限图片一律拒绝
    attachments_dir — 取（并按需创建）工作区内的材料附件目录
    prepare_attachment_target — 为一个待落盘附件选出唯一目标路径，并保证目录存在
    resolve_material_path — 把材料的相对路径解析为工作区内的真实路径

输入:
    workspace_path: str | Path — 工作区根路径
    relative_path: str — 材料的相对路径（以 "/" 分隔）
    name: str — 待落盘附件的文件名（会被取 basename，拒绝目录成分）

输出:
    attachments_dir → Path；prepare_attachment_target → Path（不存在的唯一路径）
    resolve_material_path → Path（不做存在性校验）

具体工作流:
    (1) 附件一律落在工作区内的专用子目录，绝不写工作区根目录，避免污染用户的代码仓库
    (2) guard_upload 是纯函数且不触碰文件系统，调用方据此在任何写入之前失败，
        因此被拒绝的上传不会留下半成品文件
    (3) prepare_attachment_target 先取文件名、再确保目录存在、最后挑一个不冲突的名字
    (4) 同名冲突时追加 8 位随机后缀重试，绝不覆盖既有文件
    (5) resolve_material_path 只做拼接与归一，是否存在由调用方判断

示例:
    guard_upload("shot.png", data)
    target = prepare_attachment_target(workspace, "shot.png")
    path = resolve_material_path(workspace, ".focus/attachments/shot.png")
"""

import uuid
from pathlib import Path

from focus.images import ORIGINAL_IMAGE_MAX_BYTES, image_exceeds_original_limit

MATERIAL_ATTACHMENTS_SUBDIR = ".focus/attachments"


class EmptyUpload(ValueError):
    """上传内容为空。"""


class OversizedImage(ValueError):
    """原图体积超出可接受上限。"""


def guard_upload(name: str, data: bytes) -> None:
    if not data:
        raise EmptyUpload("上传文件为空")
    if image_exceeds_original_limit(name, len(data)):
        raise OversizedImage(
            f"图片超过原图体积上限 {ORIGINAL_IMAGE_MAX_BYTES} 字节: {name}"
        )


def attachments_dir(workspace_path: str | Path) -> Path:
    directory = Path(workspace_path).resolve() / MATERIAL_ATTACHMENTS_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def prepare_attachment_target(workspace_path: str | Path, name: str) -> Path:
    return _unique_target(attachments_dir(workspace_path), Path(name).name)


def resolve_material_path(workspace_path: str | Path, relative_path: str) -> Path:
    return Path(workspace_path).resolve().joinpath(*Path(relative_path).parts)


def _unique_target(directory: Path, name: str) -> Path:
    candidate = directory / (name or "upload.bin")
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    while True:
        candidate = directory / f"{stem}-{uuid.uuid4().hex[:8]}{suffix}"
        if not candidate.exists():
            return candidate
