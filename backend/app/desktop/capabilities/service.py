"""CapabilityService 的业务实现与显式依赖端口。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.capabilities.skills import (
    build_task_skill_catalog,
    resolve_task_skills,
)
from backend.app.desktop.persistence.models import DesktopThread, DesktopWorkspace


class CapabilityServicePorts(Protocol):
    async def _get_task_entities(
        self, session: AsyncSession, task_id: str
    ) -> tuple[DesktopThread, DesktopWorkspace]: ...

    app_config: Any

    session_factory: Any


class CapabilityService:
    def __init__(self, ports: CapabilityServicePorts) -> None:
        self._ports = ports

    async def equipment(self, *, include_tools: bool = True) -> dict[str, Any]:
        ports = self._ports
        from backend.app.desktop.capabilities.model_settings import catalog_snapshot

        try:
            raw = json.loads(Path("extensions_config.json").read_text(encoding="utf-8"))
            skills = sorted(
                (
                    name
                    for name, cfg in raw.get("skills", {}).items()
                    if cfg.get("enabled")
                )
            )
        except (OSError, json.JSONDecodeError):
            skills = []
        try:
            default_model_name = ports.app_config.resolve_default_model_name()
        except ValueError:
            default_model_name = None
        return {
            "models": catalog_snapshot(ports.app_config),
            "default_model_name": default_model_name,
            "skills": skills,
            "permissions": ["read", "write", "host_command"],
            "tools": await self._equipment_tools() if include_tools else [],
            "tools_status": "loaded" if include_tools else "deferred",
        }

    async def _equipment_tools(self) -> list[dict[str, Any]]:
        from focus.tools.interfaces import ToolInfo
        from focus.tools.tools import get_available_tools

        reg = await get_available_tools()
        if os.name == "nt":
            from backend.app.desktop.browser.tools import build_browser_tools

            reg = [
                *(
                    ToolInfo(
                        name=item.name,
                        label=item.name,
                        description=item.description,
                        source="builtin",
                        build_tool=lambda item=item: item,
                    )
                    for item in build_browser_tools()
                ),
                *(item for item in reg if not item.name.startswith("browser_")),
            ]
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for info in reg:
            if info.name in seen:
                continue
            seen.add(info.name)
            result.append(
                {
                    "name": info.name,
                    "label": info.label if isinstance(info, ToolInfo) else info.name,
                    "source": info.source if isinstance(info, ToolInfo) else "builtin",
                }
            )
        return result

    async def list_task_skills(self, task_id: str) -> list[dict[str, str]]:
        ports = self._ports
        async with ports.session_factory() as session:
            _, workspace = await ports._get_task_entities(session, task_id)
        return [
            {"name": skill.name, "description": skill.description}
            for skill in build_task_skill_catalog(workspace.path).values()
        ]

    async def refresh_task_capabilities(self, task_id: str) -> dict[str, Any]:
        ports = self._ports
        from dataclasses import asdict

        from focus.mcp.cache import get_mcp_tools_cached
        from focus.tools.registry import reload_tool_registry

        async with ports.session_factory() as session:
            _, workspace = await ports._get_task_entities(session, task_id)
        custom_tools = reload_tool_registry().tools()
        tools = await get_mcp_tools_cached(workspace.path, force_refresh=True)
        return {
            "skills": await self.list_task_skills(task_id),
            "custom_tool_count": len(custom_tools),
            "mcp_tool_count": len(tools),
            "failed_servers": list(getattr(tools, "failed_servers", ())),
            "mcp_failures": [
                asdict(failure) for failure in getattr(tools, "failures", ())
            ],
        }

    def _model_supports_image_input(self, model_name: str | None) -> bool:
        ports = self._ports
        try:
            model = ports.app_config.get_model(
                model_name or ports.app_config.resolve_default_model_name()
            )
        except (KeyError, ValueError):
            return False
        return bool(model.supports_image_input)

    def _compression_context_window(self, model_name: str | None) -> int | None:
        ports = self._ports
        try:
            model = ports.app_config.get_model(
                model_name or ports.app_config.resolve_default_model_name()
            )
        except (KeyError, ValueError):
            return None
        return model.context_window

    def _require_model(self, model_name: str | None):
        ports = self._ports
        try:
            return ports.app_config.get_model(
                model_name or ports.app_config.resolve_default_model_name()
            )
        except ValueError as exc:
            raise HTTPException(
                422, {"code": "model_entry_missing", "message": str(exc)}
            ) from exc
        except KeyError as exc:
            from focus.config.app_config import available_model_names

            raise HTTPException(
                422,
                {
                    "code": "model_entry_missing",
                    "model_name": model_name,
                    "message": f"模型条目已不存在: '{model_name}'，请在界面重新选择模型；可用条目: {available_model_names(ports.app_config)}",
                },
            ) from exc

    def _validate_model_window(self, model_name: str | None, estimate: int) -> None:
        model = self._require_model(model_name)
        if model.context_window is not None and estimate > model.context_window:
            raise HTTPException(
                422,
                {
                    "code": "context_window_exceeded",
                    "estimate": estimate,
                    "limit": model.context_window,
                },
            )

    @staticmethod
    def _normalize_skill_names(value: Any) -> list[str]:
        if value is None or value == "auto":
            return []
        if not isinstance(value, list) or any(
            (not isinstance(name, str) for name in value)
        ):
            raise HTTPException(422, {"code": "invalid_skills"})
        return list(dict.fromkeys(value))

    def _freeze_skills(
        self, workspace: str | Path, selected: Any
    ) -> list[dict[str, str]]:
        snapshots, unavailable = resolve_task_skills(
            build_task_skill_catalog(workspace), self._normalize_skill_names(selected)
        )
        if unavailable:
            raise HTTPException(
                422, {"code": "skill_unavailable", "names": unavailable}
            )
        return snapshots

    def _normalize_equipment(self, equipment: dict[str, Any]) -> dict[str, Any]:
        ports = self._ports
        model_name = (
            equipment.get("model_name") or ports.app_config.resolve_default_model_name()
        )
        self._require_model(model_name)
        raw_permissions = equipment.get("permissions")
        permissions = list(
            dict.fromkeys(["read"] if raw_permissions is None else raw_permissions)
        )
        invalid = set(permissions) - {"read", "write", "host_command"}
        if invalid:
            raise HTTPException(422, f"未知权限: {', '.join(sorted(invalid))}")
        return {
            "model_name": model_name,
            "skills": self._normalize_skill_names(equipment.get("skills")),
            "permissions": permissions,
        }
