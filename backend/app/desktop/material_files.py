"""本文件对外提供材料文件在工作区内的落位、读取与预览解码装配，是附件目录约定与文本编码链的唯一归属地。

对外提供:
    MATERIAL_ATTACHMENTS_SUBDIR — 材料附件在工作区内的专用子目录（相对路径）
    PREVIEW_TEXT_MAX_BYTES — 单次预览最多读取的字节数
    TEXT_PREVIEW_ENCODINGS — 预览解码的编码尝试顺序
    TEXT_TRUNCATION_MARKER — 内容被截断时追加在正文尾部的可见标记
    EmptyUpload / OversizedImage — 落盘前把关的两类拒绝原因
    guard_upload — 落盘前把关：空内容与超限图片一律拒绝
    attachments_dir — 取（并按需创建）工作区内的材料附件目录
    prepare_attachment_target — 为一个待落盘附件选出唯一目标路径，并保证目录存在
    resolve_material_path — 把材料的相对路径解析为工作区内的真实路径
    read_material_text — 把材料按上限读成文本，并报告编码与截断

输入:
    workspace_path: str | Path — 工作区根路径
    relative_path: str — 材料的相对路径（以 "/" 分隔）
    name: str — 待落盘附件的文件名（会被取 basename，拒绝目录成分）
    path: str | Path — 已解析的材料真实路径（read_material_text）
    max_bytes: int — 预览读取上限（read_material_text，默认 PREVIEW_TEXT_MAX_BYTES）

输出:
    attachments_dir → Path；prepare_attachment_target → Path（不存在的唯一路径）
    resolve_material_path → Path（不做存在性校验）
    read_material_text → MaterialText（text / encoding / truncated / size_bytes）
    非文本内容由 read_material_text 抛 UnicodeDecodeError，由调用方翻译成 415

具体工作流:
    (1) 附件一律落在工作区内的专用子目录，绝不写工作区根目录，避免污染用户的代码仓库
    (2) guard_upload 是纯函数且不触碰文件系统，调用方据此在任何写入之前失败，
        因此被拒绝的上传不会留下半成品文件
    (3) prepare_attachment_target 先取文件名、再确保目录存在、最后挑一个不冲突的名字
    (4) 同名冲突时追加 8 位随机后缀重试，绝不覆盖既有文件
    (5) resolve_material_path 只做拼接与归一，是否存在由调用方判断
    (6) read_material_text 先按上限取字节，再按 TEXT_PREVIEW_ENCODINGS 逐级解码；
        被截断的字节尾可能破在多字节字符中间，故解码失败时逐字节回退到最后一个可解码位置

示例:
    guard_upload("shot.png", data)
    target = prepare_attachment_target(workspace, "shot.png")
    path = resolve_material_path(workspace, ".focus/attachments/shot.png")
    preview = read_material_text(path)   # 中文文本即使为 GB18030 也能正确解码
"""

from dataclasses import dataclass
import uuid
from pathlib import Path

from focus.images import ORIGINAL_IMAGE_MAX_BYTES, image_exceeds_original_limit

MATERIAL_ATTACHMENTS_SUBDIR = ".focus/attachments"

PREVIEW_TEXT_MAX_BYTES = 512 * 1024

# gb18030 是 GBK / GB2312 的超集，一条覆盖三种常见中文编码；
# utf-8-sig 同时覆盖带 BOM 与不带 BOM 的 UTF-8，并让 BOM 不进入正文。
TEXT_PREVIEW_ENCODINGS = ("utf-8-sig", "gb18030")

TEXT_TRUNCATION_MARKER = "\n\n…（内容超出预览上限，已截断）"


@dataclass(frozen=True)
class MaterialText:
    """一份材料的文本预览结果。size_bytes 是文件在磁盘上的真实大小，不是本次读取的字节数。"""

    text: str
    encoding: str
    truncated: bool
    size_bytes: int


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


def read_material_text(
    path: str | Path,
    max_bytes: int = PREVIEW_TEXT_MAX_BYTES,
) -> MaterialText:
    """把材料读成文本预览。非文本内容抛 UnicodeDecodeError，由路由翻译成 415。"""
    material_path = Path(path)
    size_bytes = material_path.stat().st_size
    with material_path.open("rb") as handle:
        # 多读 3 字节只为容纳 BOM，使小文件的 truncated 判定不被 BOM 长度干扰。
        data = handle.read(max_bytes + 3)
    truncated = size_bytes > max_bytes
    if truncated:
        data = data[:max_bytes]
    text, encoding = _decode_text(data)
    if truncated:
        text += TEXT_TRUNCATION_MARKER
    return MaterialText(
        text=text, encoding=encoding, truncated=truncated, size_bytes=size_bytes
    )


def _decode_text(data: bytes) -> tuple[str, str]:
    for encoding in TEXT_PREVIEW_ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError as error:
            # 截断点可能破在多字节字符中间，逐字节回退到最后一个可解码前缀，
            # 既不误判整份文件为二进制，也不把半个字符塞进正文。
            if error.start > 0:
                try:
                    return data[: error.start].decode(encoding), encoding
                except UnicodeDecodeError:
                    continue
            continue
    raise UnicodeDecodeError("focus-preview", data, 0, 1, "无法按已知编码解码为文本")


def _unique_target(directory: Path, name: str) -> Path:
    candidate = directory / (name or "upload.bin")
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    while True:
        candidate = directory / f"{stem}-{uuid.uuid4().hex[:8]}{suffix}"
        if not candidate.exists():
            return candidate
