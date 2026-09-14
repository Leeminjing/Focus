"""本文件对外提供权柄面的识别与派生，是「哪些持久化路径会改变未来的执行」的唯一归属地。

对外提供:
    AuthorityKind — 权柄面的类别：凭据 / 指令 / 可执行代码 / 工具集合 / 外部信任
    AuthoritySurface — 一个权柄面：类别 + 真实路径 + 其读取是否放行
    authority_surfaces(workspace) — 由判定原则派生全部权柄面
    authority_surface_for(target, surfaces) — 目标落在哪个权柄面内；不属于任何面时返回 None

输入:
    workspace: Path | None — 当前工作区根；None 表示只统计与工作区无关的权柄面
    target: Path — 已规范化的真实宿主路径
    surfaces: 序列 — 已派生的权柄面集合

输出:
    authority_surfaces → tuple[AuthoritySurface, ...]（工作区内的面排在后，便于人工核对）
    authority_surface_for → AuthoritySurface | None

具体工作流:
    (1) 判定原则是「其持久化内容能否改变未来的执行」，限定词是「存在一条无需模型请求即把内容
        送入未来执行的持久路径」——因此权柄面不是靠路径枚举，而是靠这条原则推导出的成员
    (2) 配置文件成员取自分层加载器报出的物理候选路径（两层各一，不区分是否存在），
        因为「新建该文件」与「改写该文件」同样改变未来执行
    (3) 凭据、指令、可执行代码、工具集合、外部信任五类各自成面；同一路径只登记一次
    (4) 读取是否放行由类别决定：凭据类读也待决；普通配置类读放行而写待决
    (5) 与工作区无关的面在前，工作区内的面（如 .agents/skills）在后
    (6) 候选路径在此归一为绝对路径——分层加载器报出的仓库态候选是相对当前目录的，
        而权柄面匹配一律在绝对规范化路径上比较

示例:
    surfaces = authority_surfaces(Path("C:/ws"))
    surface = authority_surface_for(Path("C:/ws/.agents/skills/x/SKILL.md"), surfaces)
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from focus.config.layered import global_home, layered_candidate_paths
from focus.security.paths import is_within

_WORKSPACE_INSTRUCTION_DIRS = (Path(".agents") / "skills",)
"""工作区内的指令目录：会被主动扫描并注入后续运行的 system prompt。"""


class AuthorityKind(StrEnum):
    """权柄面的类别：它改变未来执行的方式。"""

    CREDENTIALS = "credentials"
    INSTRUCTIONS = "instructions"
    CODE = "code"
    TOOL_SET = "tool_set"
    TRUST = "trust"


@dataclass(frozen=True)
class AuthoritySurface:
    """一个权柄面：类别、真实路径，以及其读取是否放行（写入恒待决）。"""

    kind: AuthorityKind
    path: Path
    readable: bool = True
    reason: str = ""


_LAYERED_CONFIG_SURFACES = (
    ("config.yaml", AuthorityKind.INSTRUCTIONS, "分层配置的候选路径：内容改变默认模型与可选能力的装配"),
    (
        "extensions_config.json",
        AuthorityKind.TRUST,
        "分层配置的候选路径：内容改变技能与 server 启停，并含可放宽外部工具信任的覆盖",
    ),
)
"""经分层聚合读取的配置名及其类别；它们各有全局态与仓库态两个物理候选路径。"""


def authority_surfaces(workspace: Path | None = None) -> tuple[AuthoritySurface, ...]:
    """由判定原则派生全部权柄面；同一路径只登记一次。"""
    home = global_home()
    surfaces: list[AuthoritySurface] = [
        AuthoritySurface(
            AuthorityKind.CREDENTIALS, home / ".env", readable=False,
            reason="启动期注入进程环境的凭据，读即外泄、写即换凭据",
        ),
    ]
    for name, kind, reason in _LAYERED_CONFIG_SURFACES:
        for candidate in layered_candidate_paths(name, name):
            surfaces.append(
                AuthoritySurface(kind, Path(candidate).resolve(), reason=reason)
            )
    surfaces.extend(
        [
            AuthoritySurface(
                AuthorityKind.TOOL_SET, home / "tools",
                reason="自定义工具目录：其中的模块在本进程内执行",
            ),
            AuthoritySurface(
                AuthorityKind.CODE, home / "plugins",
                reason="插件目录：入口模块在本进程内执行",
            ),
            AuthoritySurface(
                AuthorityKind.TRUST, home / "plugins-disabled.json",
                reason="插件启停偏好：决定下次加载哪些进程内代码",
            ),
            AuthoritySurface(
                AuthorityKind.INSTRUCTIONS, home / "users",
                reason="按用户的资源根：技能内容会被注入后续运行的指令",
            ),
        ]
    )
    if workspace is not None:
        for relative in _WORKSPACE_INSTRUCTION_DIRS:
            surfaces.append(
                AuthoritySurface(
                    AuthorityKind.INSTRUCTIONS, Path(workspace) / relative,
                    reason="工作区内的技能目录：会被主动扫描并注入指令，与普通工作内容不同",
                )
            )
    return tuple(dict.fromkeys(surfaces))


def authority_surface_for(
    target: Path, surfaces: Sequence[AuthoritySurface]
) -> AuthoritySurface | None:
    """目标落在哪个权柄面内；不属于任何面时返回 None。"""
    for surface in surfaces:
        if is_within(surface.path, target):
            return surface
    return None


def authority_paths(surfaces: Iterable[AuthoritySurface]) -> tuple[Path, ...]:
    """权柄面的路径集合，供需要按路径比较的调用方使用。"""
    return tuple(surface.path for surface in surfaces)
