"""策略保存准入：按冻结覆盖规则检查目录集合，返回每份策略的 UTF-8 请求占用。

目录最多使用可用输入窗口的一半；同时扣除 Patrol 固定提示/schema，并为动态
Observation 至少保留 16 KiB。预检不承诺未来任意 Observation 都能装入，最终请求仍
由 PatrolRequestCapacity 校验。写入使用事务级 advisory lock，防止并发安装穿透集合检查。
"""

import json
from types import SimpleNamespace

from fastapi import HTTPException
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select, text

from backend.app.desktop.capabilities.evolution_strategies import freeze_strategy_rows, strategy_catalog
from backend.app.desktop.persistence.models import Memory
from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopDelegationGrant
from backend.app.desktop.patrol.agent_loop.inputs.patrol_request_capacity import PatrolRequestCapacity


OBSERVATION_INPUT_RESERVE = 16384


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def strategy_input_bound(strategies):
    # Observation JSON is itself a message string: count both layers of escaping.
    return len(_json(_json(strategies)).encode("utf-8")) - len(_json("[]").encode("utf-8"))


def strategy_capacity_report(strategies, config, limits):
    # Import lazily to avoid cycles while measuring the actual Patrol contracts.
    from backend.app.desktop.patrol.agent_loop.rounds.round_orchestration import (
        PATROL_SYSTEM_CONTRACT, PatrolCognitiveStep, PatrolDecisionProposal,
    )

    capacity = PatrolRequestCapacity(config, limits)
    fixed_bound = 0
    for schema in (PatrolCognitiveStep, PatrolDecisionProposal):
        messages = [SystemMessage(content=PATROL_SYSTEM_CONTRACT),
                    HumanMessage(content='<loop_observation>{"evolution_strategies":[]}</loop_observation>')]
        if config.curation_output_method == "prompt_json":
            messages.append(HumanMessage(content=f"只返回符合此 JSON Schema 的 JSON：{_json(schema.model_json_schema())}"))
        fixed_bound = max(fixed_bound, capacity.estimate(messages, schema))
    limit = capacity.input_limit
    strategy_limit = None if limit is None else max(0, min(limit // 2, limit - fixed_bound - OBSERVATION_INPUT_RESERVE))
    catalog = strategy_catalog(strategies)
    used = strategy_input_bound(catalog)
    contributors = [{"memory_id": item["memory_id"], "title": item["title"],
                     "package_id": item.get("package", {}).get("id"),
                     "input_bound": strategy_input_bound((item,))} for item in catalog]
    return {"model_name": config.name, "input_bound": used, "strategy_input_limit": strategy_limit,
            "request_input_limit": limit, "output_reserve": capacity.output_reserve,
            "fixed_input_bound": fixed_bound, "observation_input_reserve": OBSERVATION_INPUT_RESERVE,
            "collection_overhead": max(0, len(strategies) - 1),
            "fits": strategy_limit is None or used <= strategy_limit,
            "strategies": sorted(contributors, key=lambda item: (-item["input_bound"], item["memory_id"]))}


class StrategyCapacityAdmission:
    def __init__(self, rows, profiles, app_config):
        self.rows = list(rows)
        self.profiles = profiles
        self.app_config = app_config
        self.scopes = {None, *(row.workspace_id for row in rows), *(scope for scope, _, _ in profiles)}
        self.before = {scope: freeze_strategy_rows(rows, scope) for scope in self.scopes}

    @classmethod
    async def load(cls, session, app_config, *, lock=False):
        if lock:
            # One lock across all scopes: a global install affects every local set.
            await session.execute(text("SELECT pg_advisory_xact_lock(1937011303, 1)"))
        rows = (await session.scalars(select(Memory).where(Memory.category == "evolution_strategy")
                                      .execution_options(populate_existing=True))).all()
        profiles = (await session.execute(select(AgentLoop.workspace_id, AgentLoop.equipment, LoopDelegationGrant.budgets)
            .join(LoopDelegationGrant, LoopDelegationGrant.loop_id == AgentLoop.loop_id)
            .where(LoopDelegationGrant.status == "active", AgentLoop.status.not_in(("completed", "stopped", "failed"))))).all()
        return cls(rows, profiles, app_config)

    def reports(self, rows, *, workspace_id=None):
        scopes = self.scopes | {workspace_id, *(row.workspace_id for row in rows)}
        reports = []
        for scope in sorted(scopes, key=lambda value: value or ""):
            frozen = freeze_strategy_rows(rows, scope)
            previous = self.before.get(scope, self.before[None])
            if frozen == previous and scope != workspace_id:
                continue
            profiles = [(None, {})] + [(equipment.get("patrol_model_name") or equipment.get("model_name"), limits)
                for profile_scope, equipment, limits in self.profiles if profile_scope == scope]
            for model_name, limits in profiles:
                config = (self.app_config.get_model(model_name or self.app_config.resolve_default_model_name())
                    if self.app_config is not None else SimpleNamespace(name="default", context_window=None,
                        curation_max_output_tokens=8192, curation_output_method="prompt_json"))
                report = strategy_capacity_report(frozen, config, limits)
                reports.append({**report, "workspace_id": scope, "previous_input_bound": strategy_input_bound(strategy_catalog(previous))})
        return reports

    def check(self, rows):
        for report in self.reports(rows):
            if report["fits"] or report["input_bound"] <= report["previous_input_bound"]:
                continue  # Existing oversized sets can still be reduced or disabled.
            contributors = "；".join(f'{item["title"]} ({item["package_id"] or item["memory_id"]}): {item["input_bound"]}'
                                     for item in report["strategies"])
            raise HTTPException(422, f'生效策略集合超出 Patrol 请求容量（UTF-8 序列化保守输入上界）。'
                f'范围：{report["workspace_id"] or "全局"}；模型：{report["model_name"]}；'
                f'策略目录占用 {report["input_bound"]} / {report["strategy_input_limit"]}；'
                f'请求输入上限 {report["request_input_limit"]}，输出预留 {report["output_reserve"]}。'
                f'各策略占用：{contributors}。请缩短正文、停用部分策略或先以停用状态保存。')
