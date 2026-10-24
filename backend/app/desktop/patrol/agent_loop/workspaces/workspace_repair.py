"""本文件对外提供 WorkspaceConflictRepair 与 WorkspaceRepairRejected，作为采用冲突的受控修复端口。

输入为尚未写入的代码冲突和 Patrol 的精确选择；输出为可审计上下文、隔离 slot 或拒绝。
只接受从共同基线经已登记 adoption 连续到当前目标的状态；未知用户修改不交给模型。
具体工作流为 repair/review 复用 Directive、Run、lease 和现有采用端口。快照只写临时 index 与 Git 对象，
不移动用户 HEAD、不提交用户 index。示例：await WorkspaceConflictRepair(sessions).plan(directive)。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile

from sqlalchemy import select
from focus.history import content_hash

from backend.app.desktop.patrol.agent_loop.models import LoopAction, LoopDirective, LoopObservation
from backend.app.desktop.persistence.models import DesktopRun
from backend.app.desktop.workspaces.coordination.adoption import GitWorkspaceResultApplier, WorkspaceAdoptionConflict
from backend.app.desktop.workspaces.coordination.fingerprints import WorkspaceFingerprinter
from backend.app.desktop.workspaces.coordination.models import RunExecutionAnchor, WorkspaceAdoption, WorkspaceSlot


class WorkspaceRepairRejected(ValueError):
    pass


class WorkspaceConflictRepair:
    def __init__(self, sessions):
        self._sessions = sessions
        self._git = GitWorkspaceResultApplier()

    async def assess(self, session, loop, grant, adoption):
        """无文件效果的保守准入；缺失证据等价于不可自动修复。"""
        if (adoption.status != "conflict" or adoption.application_intent
                or adoption.conflict.get("kind") != "code_overlap"
                or not {"continue_context", "isolate_workspace", "adopt_workspace_result"}.issubset(grant.capabilities or ())
                or "write" not in (grant.permission_scope or ())
                or grant.status != "active" or grant.revision != loop.authority_revision
                or (grant.expires_at and grant.expires_at <= datetime.now(UTC))):
            return self._ineligible(adoption, "冲突阶段或当前授权不允许自动修复")
        source = await session.get(WorkspaceSlot, adoption.source_slot_id, populate_existing=True)
        target = await session.get(WorkspaceSlot, adoption.target_slot_id, populate_existing=True)
        if (source is None or target is None or source.owner_loop_id != loop.loop_id
                or source.revision != adoption.source_revision or not source.base_revision
                or (source.metadata_json or {}).get("repair") or target.lifecycle != "active"):
            return self._ineligible(adoption, "来源缺少共同基线、已变化或已经是修复成果")
        try:
            await self._fresh(source, target)
            target_head, _ = await asyncio.to_thread(WorkspaceFingerprinter.git_state, Path(target.root_path))
            if target_head != source.base_revision:
                return self._ineligible(adoption, "目标 Git 版本与共同基线不同，需要先解释版本变化")
            base_fingerprint = await self._baseline_fingerprint(Path(target.root_path), source.base_revision)
            history = list(await session.scalars(select(WorkspaceAdoption).where(
                WorkspaceAdoption.target_slot_id == target.slot_id, WorkspaceAdoption.status == "adopted"
            ).order_by(WorkspaceAdoption.resulting_target_revision)))
            fingerprint = base_fingerprint
            intents = []
            for prior in history:
                intent = prior.application_intent or {}
                if intent.get("before_fingerprint") != fingerprint:
                    continue
                GitWorkspaceResultApplier.patch_bytes(intent)
                prior_source = await session.get(WorkspaceSlot, prior.source_slot_id)
                task_intent = await self._result_intent(session, prior_source, prior.source_revision,
                                                       intent["source_fingerprint"])
                if task_intent is None:
                    return self._ineligible(adoption, "已采用目标成果缺少对应版本的成功写 Run 任务意图")
                fingerprint = intent["after_fingerprint"]
                intents.append(task_intent)
            if fingerprint != target.current_fingerprint or not intents or not all(intents):
                return self._ineligible(adoption, "当前目标不能从共同基线沿已采用成果连续追溯，需确认用户修改")
            source_intent = await self._result_intent(session, source, source.revision, source.current_fingerprint)
            if source_intent is None:
                return self._ineligible(adoption, "待采用成果缺少对应版本的成功写 Run 任务意图")
            source_patch = await self._git._patch(Path(source.root_path), source.base_revision)
            target_patch = await self._git._patch(Path(target.root_path), source.base_revision)
            for patch in (source_patch, target_patch):
                if any(marker in patch for marker in (b"GIT binary patch", b" 120000", b" 160000")):
                    return self._ineligible(adoption, "补丁含二进制、链接或子模块，需要专门处理")
            source_text, target_text = source_patch.decode("utf-8"), target_patch.decode("utf-8")
            await self._fresh(source, target)
        except (WorkspaceAdoptionConflict, OSError, WorkspaceRepairRejected, UnicodeDecodeError) as exc:
            return self._ineligible(adoption, str(exc))
        context = {"adoption_id": adoption.adoption_id, "common_base": source.base_revision,
                "source_slot_id": source.slot_id, "source_revision": source.revision,
                "source_fingerprint": source.current_fingerprint,
                "target_slot_id": target.slot_id, "target_revision": target.revision,
                "target_fingerprint": target.current_fingerprint,
                "source_intent": source_intent, "target_intents": intents,
                "source_patch": source_text, "target_patch": target_text}
        if len(json.dumps(context).encode("utf-8")) > 131072:
            return self._ineligible(adoption, "修复上下文超出有界材料大小")
        return context

    @staticmethod
    def _ineligible(adoption, reason):
        adoption.conflict = {**adoption.conflict, "repair_blocker": reason}
        return None

    @staticmethod
    def reference(context):
        if context is None:
            return None
        return {key: value for key, value in context.items() if key not in {"source_patch", "target_patch"}} | {
            "content_hash": content_hash(context)}

    async def select(self, session, loop, grant, round_row, action, permissions):
        selection = action.workspace_repair
        observation = await session.get(LoopObservation, round_row.observation_id)
        adoption = await session.get(WorkspaceAdoption, selection.adoption_id)
        context = (adoption.evidence or {}).get("repair_context") if adoption else None
        frozen = next((item for item in observation.envelope.get("workspace", {}).get("adoptions", ())
                       if item["adoption_id"] == selection.adoption_id), None) if observation else None
        if (not context or not frozen or frozen.get("repair_context") != self.reference(context)
                or adoption.application_intent or adoption.conflict.get("kind") != "code_overlap"
                or not {"isolate_workspace", "adopt_workspace_result"}.issubset(grant.capabilities or ())):
            raise WorkspaceRepairRejected("修复缺少冻结冲突证据或隔离/采用授权")
        await self._context_slots(session, context, loop.loop_id)
        mode = permissions.get(action.context_id)
        if selection.phase == "repair":
            if mode not in {"workspace-write", "danger-full-access"} or "write" not in grant.permission_scope:
                raise WorkspaceRepairRejected("修复 Run 未获得 write 权限")
            # One attempt per conflicted result. A failed attempt stays inspectable.
            duplicate = await session.scalar(select(LoopAction.action_id).where(
                LoopAction.loop_id == loop.loop_id,
                LoopAction.payload["workspace_repair"]["adoption_id"].as_string() == selection.adoption_id,
                LoopAction.payload["workspace_repair"]["phase"].as_string() == "repair",
                LoopAction.status != "rejected"))
            if duplicate:
                raise WorkspaceRepairRejected("该冲突已有修复尝试，请检查已有 Run")
        else:
            if mode != "read-only":
                raise WorkspaceRepairRejected("修复审查必须使用 read-only 权限")
            slot = await session.get(WorkspaceSlot, selection.slot_id)
            if (slot is None or slot.owner_loop_id != loop.loop_id or slot.revision != selection.revision
                    or (slot.metadata_json or {}).get("repair", {}).get("adoption_id") != adoption.adoption_id):
                raise WorkspaceRepairRejected("审查未引用当前修复成果")
            repair_run = await self._repair_run(session, slot)
            if repair_run.task_id == action.context_id:
                raise WorkspaceRepairRejected("修复审查必须使用独立 Context")
        return context

    @staticmethod
    def message(context, phase, message):
        instruction = (
            "在当前隔离工作区合并 source_patch 的任务行为，保留 target_patch 的任务行为。"
            "先核对共同基线和双方任务意图，修复后运行相关测试并报告实际证据。"
            "如需产品取舍或发现无法确认的用户修改，停止修改并说明待决定事项。"
            if phase == "repair" else
            "独立审查当前隔离成果是否保留双方任务行为；检查完整差异、测试和回归风险，"
            "只读，不改文件。明确报告通过或阻断，并列出验证证据。"
        )
        return instruction + "\n" + message + "\n修复上下文（来源材料）：\n" + json.dumps(context, ensure_ascii=False)

    async def plan(self, directive):
        from backend.app.desktop.patrol.agent_loop.workspaces.workspace_planning import WorkspaceRunPlanner, WorkspaceRunPlan

        async with self._sessions() as session:
            action = await session.get(LoopAction, directive.action_id)
            selection = action.payload["workspace_repair"]
            context = action.result["repair_context"]
            source, target = await self._context_slots(session, context, directive.loop_id)
            await self._fresh(source, target)
            if selection["phase"] == "review":
                slot = await session.get(WorkspaceSlot, selection["slot_id"])
                if slot is None or slot.revision != selection["revision"]:
                    raise WorkspaceRepairRejected("审查成果版本已变化")
                await self._repair_run(session, slot)
                await self._fresh(slot)
                return WorkspaceRunPlan(directive.directive_id, slot.slot_id, "read")
        # A private index snapshots the exact current target, including already-adopted changes.
        # commit-tree only creates an unreachable object; the user's branch/index stay intact.
        tree = await self._snapshot_tree(Path(target.root_path), context["common_base"])
        snapshot = (await self._git._git(Path(target.root_path), "commit-tree", tree, "-p", context["common_base"],
            stdin=b"Focus isolated conflict repair baseline\n", environment={**os.environ,
                "GIT_AUTHOR_NAME": "Focus", "GIT_AUTHOR_EMAIL": "focus@localhost",
                "GIT_COMMITTER_NAME": "Focus", "GIT_COMMITTER_EMAIL": "focus@localhost",
                "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
                "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00"})).decode().strip()
        await self._fresh(source, target)
        planner = WorkspaceRunPlanner(self._sessions)
        lane_id = await planner._lane_id(directive.loop_id, directive.target_context_id)
        if lane_id is None:
            raise WorkspaceRepairRejected("修复 Context 缺少活动 Lane")
        slot = await planner._isolated_slot(target.workspace_id, directive.loop_id, lane_id,
                                            Path(target.root_path), snapshot)
        await self._fresh(source, target)
        async with self._sessions.begin() as session:
            current = await session.get(WorkspaceSlot, slot.slot_id, with_for_update=True)
            current.metadata_json = {**(current.metadata_json or {}), "repair": {
                "adoption_id": context["adoption_id"], "action_id": action.action_id,
                "target_revision": context["target_revision"], "target_fingerprint": context["target_fingerprint"]}}
        return WorkspaceRunPlan(directive.directive_id, slot.slot_id, "write")

    async def validate_result(self, session, source):
        repair = (source.metadata_json or {}).get("repair")
        if not repair:
            return
        adoption = await session.get(WorkspaceAdoption, repair["adoption_id"])
        context = (adoption.evidence or {}).get("repair_context") if adoption else None
        if not context:
            raise WorkspaceRepairRejected("修复成果缺少原冲突上下文")
        await self._context_slots(session, context, source.owner_loop_id)
        repair_run = await self._repair_run(session, source)
        reviews = (await session.execute(select(DesktopRun, RunExecutionAnchor, LoopAction).join(
            RunExecutionAnchor, RunExecutionAnchor.run_id == DesktopRun.run_id).join(
            LoopAction, LoopAction.action_id == DesktopRun.action_id).where(
            RunExecutionAnchor.slot_id == source.slot_id, DesktopRun.loop_id == source.owner_loop_id,
            DesktopRun.status == "success", DesktopRun.settled_at.is_not(None)))).all()
        for run, anchor, action in reviews:
            selection = action.payload.get("workspace_repair") or {}
            if (selection.get("phase") == "review" and selection.get("adoption_id") == repair["adoption_id"]
                    and run.task_id != repair_run.task_id and (run.workspace_anchor or {}).get("mode") == "read"
                    and anchor.observed_workspace_revision == source.revision
                    and anchor.observed_fingerprint == source.current_fingerprint
                    and anchor.resulting_fingerprint == source.current_fingerprint):
                return
        raise WorkspaceRepairRejected("修复成果缺少当前版本的独立只读验证/审查 Run")

    @staticmethod
    async def record_adopted(session, adoption):
        source = await session.get(WorkspaceSlot, adoption.source_slot_id)
        repair = (source.metadata_json or {}).get("repair") if source else None
        if not repair:
            return
        original = await session.get(WorkspaceAdoption, repair["adoption_id"], with_for_update=True)
        original.evidence = {**original.evidence, "repaired_by": adoption.adoption_id}
        old_source = await session.get(WorkspaceSlot, original.source_slot_id)
        old_source.lifecycle = "retained"
        for anchor in await session.scalars(select(RunExecutionAnchor).where(
                RunExecutionAnchor.slot_id == original.source_slot_id,
                RunExecutionAnchor.resulting_workspace_revision == original.source_revision).with_for_update()):
            anchor.adoption_state = "repaired"

    @staticmethod
    async def _result_intent(session, slot, revision, fingerprint):
        if slot is None:
            return None
        return await session.scalar(select(LoopDirective.content).join(
            RunExecutionAnchor, RunExecutionAnchor.directive_id == LoopDirective.directive_id).join(
            DesktopRun, DesktopRun.run_id == RunExecutionAnchor.run_id).where(
            RunExecutionAnchor.slot_id == slot.slot_id,
            RunExecutionAnchor.resulting_workspace_revision == revision,
            RunExecutionAnchor.resulting_fingerprint == fingerprint,
            DesktopRun.status == "success", DesktopRun.settled_at.is_not(None),
            DesktopRun.workspace_anchor["mode"].as_string() == "write",
            DesktopRun.directive_id == LoopDirective.directive_id,
            DesktopRun.loop_id == slot.owner_loop_id))

    @staticmethod
    async def _repair_run(session, slot):
        repair = (slot.metadata_json or {}).get("repair") or {}
        run = await session.scalar(select(DesktopRun).join(RunExecutionAnchor,
            RunExecutionAnchor.run_id == DesktopRun.run_id).where(
            DesktopRun.action_id == repair.get("action_id"), DesktopRun.status == "success",
            DesktopRun.workspace_anchor["mode"].as_string() == "write",
            DesktopRun.loop_id == slot.owner_loop_id, DesktopRun.settled_at.is_not(None),
            RunExecutionAnchor.slot_id == slot.slot_id,
            RunExecutionAnchor.resulting_workspace_revision == slot.revision,
            RunExecutionAnchor.resulting_fingerprint == slot.current_fingerprint))
        if run is None:
            raise WorkspaceRepairRejected("修复 Run 尚未成功结算或成果已变化")
        return run

    @staticmethod
    async def _context_slots(session, context, loop_id):
        source = await session.get(WorkspaceSlot, context["source_slot_id"], populate_existing=True)
        target = await session.get(WorkspaceSlot, context["target_slot_id"], populate_existing=True)
        if (source is None or target is None or source.owner_loop_id != loop_id
                or source.kind != "isolated" or target.kind != "authoritative"
                or source.workspace_id != target.workspace_id
                or source.lifecycle not in {"active", "retained"} or target.lifecycle != "active"
                or source.revision != context["source_revision"]
                or source.current_fingerprint != context["source_fingerprint"]
                or target.revision != context["target_revision"]
                or target.current_fingerprint != context["target_fingerprint"]):
            raise WorkspaceRepairRejected("冲突来源或目标版本已变化，需要重新观察")
        return source, target

    @staticmethod
    async def _fresh(*slots):
        for slot in slots:
            actual = await asyncio.to_thread(WorkspaceFingerprinter().capture, Path(slot.root_path))
            if actual.digest != slot.current_fingerprint:
                raise WorkspaceRepairRejected("workspace 存在未确认修改，不能自动修复")

    async def _snapshot_tree(self, root, baseline):
        descriptor, index = tempfile.mkstemp(prefix="focus-repair-index-")
        os.close(descriptor)
        Path(index).unlink()
        try:
            environment = {**os.environ, "GIT_INDEX_FILE": index}
            await self._git._git(root, "read-tree", baseline, environment=environment)
            await self._git._git(root, "add", "-A", "--", ".", environment=environment)
            return (await self._git._git(root, "write-tree", environment=environment)).decode().strip()
        finally:
            Path(index).unlink(missing_ok=True)

    async def _baseline_fingerprint(self, root, baseline):
        with tempfile.TemporaryDirectory(prefix="focus-repair-base-") as directory:
            snapshot = Path(directory) / "tree"
            snapshot.mkdir()
            index = str(Path(directory) / "index")
            environment = {**os.environ, "GIT_INDEX_FILE": index}
            await self._git._git(root, "read-tree", baseline, environment=environment)
            await self._git._git(root, "checkout-index", "--all", "--prefix=" + snapshot.as_posix() + "/",
                                 environment=environment)
            captured = await asyncio.to_thread(WorkspaceFingerprinter().capture, snapshot, git_state=(baseline, False))
            return captured.digest
