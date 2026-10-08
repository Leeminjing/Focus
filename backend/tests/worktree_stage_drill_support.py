"""本文件对外提供 stage_observations 和 accepts_stage_decision 的 Patrol 阶段演练数据。

输入为基础、独立模块、公共文件或成果采用场景；输出为仅含合成材料的冻结 Observation 和可检查的
既有 action 选择。流程为通过真实 StructuredPatrolDecisionModel 处理这些输入，再检查当前阶段的
任务边界；不连接业务数据库，不授予执行权。示例：stage_observations()["modules"]。
"""

from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope


def stage_observations():
    frontier = tuple({"context_id": context, "revision_id": f"revision-{context}", "role": role,
        "message_evidence_preview": [{"message_id": f"message-{context}", "role": "user", "content": text}]}
        for context, role, text in (
            ("primary", "primary", "负责基础、接口和最终集成；业务模块交给各自 Context。"),
            ("module-a", "side", "A 仅修改 src/a.py，消费已验证的 interface.py，独立测试 tests/a。"),
            ("module-b", "side", "B 仅修改 src/b.py，消费已验证的 interface.py，独立测试 tests/b。")))
    common = dict(loop_id="drill", loop_revision=1, round_id="round", goal_revision=1,
        authority_revision=1, observed_frontier_hash="a" * 64,
        mission={"outcome": "完成 A/B 两个模块后集成，遵守接口依赖",
            "boundaries": {"required_invariants": ["基础和共享接口须先验证", "并行写只能用独立 Git worktree",
                "模块成果须逐个显式采用后才集成"], "in_scope": ["可在当前空项目初始化 Git 并提交已验证的基础文件"]}},
        grant={"grant_id": "grant", "holder_id": "patrol", "capabilities": ["continue_context", "spawn_context",
            "isolate_workspace", "adopt_workspace_result"], "permission_scope": ["read", "write"],
            "context_scope": [item["context_id"] for item in frontier]}, portfolio_frontier=frontier,
        workspace={"slot_id": "authority", "revision": 1, "git_revision": "b" * 40,
            "git_dirty": False, "isolation_authorized": True}, budget={"limits": {"max_concurrent_runs": 2}},
        stable_results=())
    foundation = LoopObservationEnvelope(**{**common, "workspace": {**common["workspace"], "git_revision": None},
        "stable_results": ({"context_id": "primary", "status": "success", "workspace_result": {
            "evidence": "项目为空，接口尚未确定；先完成基础和接口验证，再准备已授权 Git baseline。"}},)})
    modules = LoopObservationEnvelope(**{**common, "stable_results": ({"context_id": "primary", "status": "success",
        "workspace_result": {"evidence": "interface.py 和基础测试已验证并提交；A/B 无相互依赖，各自测试可独立运行，集成尚须等待 A/B 成果。"}},),
        "expansion_assessment": {"level": "required", "opportunities": tuple({"opportunity_id": key * 64,
            "purpose": purpose, "work_order": work, "workspace_mode": "isolated_write"}
            for key, purpose, work in (("c", "module-a", "只实现 A，接口已验证，可独立验收"),
                ("d", "module-b", "只实现 B，接口已验证，可独立验收"),
                ("e", "integration", "依赖 A/B 成果已逐个采用；当前尚无成果，须留待后续阶段")))}})
    shared = LoopObservationEnvelope(**{**common, "stable_results": ({"context_id": "primary", "status": "success",
        "workspace_result": {"evidence": "新增需求要求 A/B 同时修改同一 interface.py 的协议，当前新接口未确定；先由 primary 串行稳定接口，不能并行派发实现。"}},)})
    adoption = LoopObservationEnvelope(**{**common, "workspace": {**common["workspace"], "isolated_slots": (
        {"slot_id": "slot-a", "revision": 2, "base_revision": "b" * 40, "lifecycle": "active"},
        {"slot_id": "slot-b", "revision": 2, "base_revision": "b" * 40, "lifecycle": "active"}), "adoptions": []},
        "stable_results": tuple({"context_id": context, "status": "success", "workspace_anchor": {"slot_id": slot},
            "workspace_result": {"revision": 2, "evidence": "模块及独立测试已通过，结果在隔离目录，尚未进入权威工作区"}}
            for context, slot in (("module-a", "slot-a"), ("module-b", "slot-b")))})
    return {"foundation": foundation, "modules": modules, "shared_interface": shared, "adoption": adoption}


def accepts_stage_decision(stage, actions):
    if stage in {"foundation", "shared_interface"}:
        return len(actions) == 1 and actions[0].action == "continue_context" and actions[0].context_id == "primary"
    if stage == "modules":
        return {action.opportunity_id for action in actions if action.action == "spawn_context"} == {"c" * 64, "d" * 64} and len(actions) == 2
    return len(actions) == 1 and actions[0].action == "adopt_workspace_result" and actions[0].source_slot_id in {"slot-a", "slot-b"}
