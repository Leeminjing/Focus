"""本文件对外提供材料附件目录与工作区内路径解析。

对外提供:
    MATERIAL_ATTACHMENTS_SUBDIR — 材料附件在工作区内的专用子目录（相对路径）
    attachments_dir — 取（并按需创建）工作区内的材料附件目录
    resolve_material_path — 把材料的相对路径解析为工作区内的真实路径

输入:
    workspace_path: str | Path — 工作区根路径
    relative_path: str — 材料的相对路径（以 "/" 分隔）
输出为附件目录 Path 或规范化后的材料 Path。

具体工作流:
    (1) 附件一律落在工作区内的专用子目录，绝不写工作区根目录，避免污染用户的代码仓库
    (2) 相对路径解析后必须仍位于工作区内，拒绝目录逃逸
    (3) 上传的资源限制、独占命名与补偿由 MaterialUploadService 唯一负责

示例:
    directory = attachments_dir(workspace)
    path = resolve_material_path(workspace, ".focus/attachments/shot.png")
"""

from pathlib import Path

MATERIAL_ATTACHMENTS_SUBDIR = ".focus/attachments"


def attachments_dir(workspace_path: str | Path) -> Path:
    directory = Path(workspace_path).resolve() / MATERIAL_ATTACHMENTS_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def resolve_material_path(workspace_path: str | Path, relative_path: str) -> Path:
    workspace = Path(workspace_path).resolve()
    resolved = workspace.joinpath(*Path(relative_path).parts).resolve()
    if not resolved.is_relative_to(workspace):
        raise ValueError(f"材料路径超出工作区: {relative_path}")
    return resolved
