"""本文件对外提供正式只读 Lane 及五类缺失资源的工具效果回归。

输入为隔离 PostgreSQL 的 Loop/Lane、真实工具和临时插件项目；输出为可读、拒写且无命令效果，以及缺失后合法创建恢复的证据。
具体工作流为从正式 membership 和明确的 Patrol Directive 来源解析角色装备，通过生产上下文组装调用真实文件和 Shell 工具；缺失资源保留错误配对，授权创建后重读。
示例：pytest backend/tests/test_loop_tool_boundaries.py；不操作真实 Vault、不把 fixture 声称为已安装 Obsidian。
"""

import asyncio
from datetime import UTC, datetime
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.directive_equipment import resolve_directive_equipment
from backend.app.desktop.agent_loop.models import AgentLoop, LoopContextMembership, LoopDelegationGrant
from backend.app.desktop.context_curation.models import CurationLane
from backend.tests.runtime_context_support import tool_runtime
from focus.security.policy import AccessMode
from focus.tools.builtins.workspace_tools import read_file, list_files, write_file, powershell, select_workspace_tools
from test_agent_loop_round_liveness import _seed_loop, _stop


@pytest.mark.usefixtures("isolated_postgres_database")
def test_formal_read_only_lane_rejects_write_and_shell_even_with_full_user_mode(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="readonly-role", started_at=datetime.now(UTC))
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                loop.equipment = {"permissions": ["read", "write", "host_command"], "access_mode": "danger-full-access", "model_name": "model-a"}
                grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.status == "active"))
                grant.permission_scope = list(loop.equipment["permissions"])
                member = await session.scalar(select(LoopContextMembership).where(LoopContextMembership.loop_id == loop.loop_id))
                lane = await session.get(CurationLane, member.lane_id)
                lane.lane_policy = {}
                directive = SimpleNamespace(target_context_id=member.context_id, origin_kind="patrol")
                primary = await resolve_directive_equipment(session, loop, directive)
                assert member.role == "primary" and primary["permissions"] == ["read", "write", "host_command"]
                assert primary["access_mode"] == "danger-full-access"
                member.role = "derived"
                derived = await resolve_directive_equipment(session, loop, directive)
                assert derived["permissions"] == ["read"] and derived["access_mode"] == "read-only"
                member.role = "primary"
                lane.lane_policy = {**(lane.lane_policy or {}), "workspace_mode": "read_only"}
                equipment = await resolve_directive_equipment(session, loop, directive)
            assert equipment["permissions"] == ["read"] and equipment["access_mode"] == "read-only"
            runtime = tool_runtime(agent_id="readonly-reviewer", task_id=fixture["context_id"], workspace=str(tmp_path), permissions=tuple(equipment["permissions"]), access_mode=AccessMode(equipment["access_mode"]))
            existing = tmp_path / "note.md"
            existing.write_text("original", encoding="utf-8")
            assert read_file.func(path=str(existing), runtime=runtime) == "original"
            marker = tmp_path / "forbidden" / "result.md"
            with pytest.raises(PermissionError, match="write"):
                write_file.func(path=str(marker), content="escaped", runtime=runtime)
            with pytest.raises(PermissionError, match="host_command"):
                powershell.func(command=f"[IO.File]::WriteAllText('{marker}', 'escaped')", runtime=runtime)
            assert not marker.parent.exists() and existing.read_text(encoding="utf-8") == "original"
            assert {item.name for item in select_workspace_tools(equipment["permissions"])} == {"read_file", "list_files"}
        finally:
            if fixture:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("resource,directory", [("package.json", False), ("src/tools", True),
    ("node_modules/obsidian/obsidian.d.ts", False), ("node_modules/obsidian", True), (".git", True)])
def test_missing_resource_is_paired_failure_then_authorized_creation_recovers(tmp_path, resource, directory):
    async def run():
        runtime = tool_runtime(agent_id="resource-recovery", task_id="resource-fixture", workspace=str(tmp_path), permissions=("read", "write"), access_mode=AccessMode.DANGER_FULL_ACCESS)
        tool = list_files if directory else read_file
        for index in range(2):
            result = await tool.ainvoke({"type": "tool_call", "id": f"missing-{index}", "name": tool.name,
                "args": {"path": resource, "runtime": runtime}})
            assert result.status == "error" and result.name == tool.name and result.tool_call_id == f"missing-{index}"
            assert "不存在" in result.content and resource.replace("/", "\\") in result.content.replace("/", "\\")
            assert not (tmp_path / resource).exists()
        created = resource + "/fixture.md" if directory else resource
        write_file.func(path=created, content="fixture-created", runtime=runtime)
        recovered = await tool.ainvoke({"type": "tool_call", "id": "recovered", "name": tool.name,
            "args": {"path": resource, "runtime": runtime}})
        assert recovered.status == "success" and recovered.name == tool.name and recovered.tool_call_id == "recovered"
        assert ("fixture.md" if directory else "fixture-created") in recovered.content
    asyncio.run(run())
