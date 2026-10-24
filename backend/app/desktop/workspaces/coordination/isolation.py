r"""本文件对外提供 WorkspaceIsolationProvider 协议、IsolationRequest 与 IsolationResult。

输入为权威 slot、Loop/Lane owner、公共 Git baseline 和目标根目录；输出为拥有明确物理路径与
清理句柄的隔离 workspace。具体工作流为 provider 校验能力和配额、创建隔离副本并返回审计事实，
调用方再持久化 WorkspaceSlot。示例：`result = await provider.prepare(request)`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class IsolationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    source_root: Path
    target_root: Path
    baseline: str = Field(min_length=1)
    loop_id: str = Field(min_length=1)
    lane_id: str = Field(min_length=1)


class IsolationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    root_path: Path
    provider: str
    baseline: str
    cleanup_token: str


class WorkspaceIsolationProvider(Protocol):
    async def prepare(self, request: IsolationRequest) -> IsolationResult: ...

    async def remove(self, result: IsolationResult) -> None: ...
