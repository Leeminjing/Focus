r"""本文件验证 Live Loop 的声明式文件头、职责拆分和组合根边界。

输入为新增后端/前端源文件与 app.js/live_api.py 文本；输出为头部五要素、Snapshot 传输层无领域装配依赖、app.js 无 reducer/重连/级联刷新逻辑的断言。
具体工作流为静态读取源码，不执行应用或修改文件；示例：`pytest backend/tests/test_loop_architecture_acceptance.py`。
"""

from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
AGENT_LOOP_MODULES = """
completion/completion_policy resources/context_run_pool curation/curator_assignments execution/directive_causality execution/directive_lifecycle journal/event_contract
journal/event_journal facts/fact_identity facts/fact_lifecycle facts/fact_materializer facts/fact_models facts/fact_projector
facts/fact_verification feature_flags governance/intervention_lifecycle journal/journal_models journal/lineage_events facts/fact_event_materializer
journal/portfolio_events execution/run_activity_bridge lifecycle/supervisor_registry live/live_access live/live_api live/live_checkpoint
live/live_history projections/live_projection_contract projections/live_projection_projector projections/live_projection_reducer live/live_routes projections/live_snapshot_overlay
facts/materialized_fact_query facts/materialized_fact_sources missions/mission_authority missions/mission_contract missions/mission_history missions/mission_models
missions/mission_repository missions/mission_service governance/ownership sessions/patrol_audit sessions/patrol_runtime sessions/patrol_session_models
sessions/patrol_session_repository sessions/patrol_session_state patrol_contract curation/publication_queue lifecycle/runtime_convergence supervisor
""".split()
LOOP_TEST_MODULES = """
test_loop_architecture_acceptance test_loop_event_journal test_loop_execution_ownership test_loop_fact_source_resilience
test_loop_feature_flags test_loop_fencing test_loop_live_acceptance test_loop_live_api_contract test_loop_live_projection
test_loop_live_scaling test_loop_materialized_facts test_loop_mission_contract test_loop_multi_concurrency test_loop_runtime_pools
test_loop_settlement_and_lineage test_loop_supervisor
""".split()
DESKTOP_MODULES = """
loop-legacy-connection loop-live-connection loop-live-projection.test loop-live-reducer loop-live-schema
loop-live-selectors loop-live-store loop-mission-editor
""".split()
SOURCE_FILES = (
    *(f"backend/app/desktop/patrol/agent_loop/{name}.py" for name in AGENT_LOOP_MODULES),
    *(f"backend/tests/{name}.py" for name in LOOP_TEST_MODULES),
    *(f"desktop/src/renderer/features/loop/{name}.js" for name in DESKTOP_MODULES),
    "desktop/src/renderer/features/loop/loop-console.css",
)


@pytest.mark.parametrize("relative_path", SOURCE_FILES)
def test_new_source_files_have_declarative_headers(relative_path: str) -> None:
    header = "\n".join((ROOT / relative_path).read_text(encoding="utf-8").splitlines()[:24])
    for phrase in ("本文件", "输入", "输出", "具体工作流", "示例"):
        assert phrase in header, f"{relative_path} 缺少文件头字段 {phrase}"


@pytest.mark.parametrize("relative_path", SOURCE_FILES)
def test_new_source_files_do_not_scatter_explanatory_comments(relative_path: str) -> None:
    lines = (ROOT / relative_path).read_text(encoding="utf-8").splitlines()[12:]
    comments = [line.strip() for line in lines if line.lstrip().startswith(("#", "//", "/*")) and not line.lstrip().startswith(("# noqa", "# type:"))]
    assert comments == [], f"{relative_path} 文件头之后存在说明性注释: {comments[:3]}"


def test_live_api_transport_does_not_own_domain_overlay_queries() -> None:
    source = (ROOT / "backend/app/desktop/patrol/agent_loop/live/live_api.py").read_text(encoding="utf-8")
    for forbidden in ("LoopMissionRevision", "LoopContextMembership", "DesktopRun", "LoopFact", "PortfolioRevision"):
        assert forbidden not in source
    assert "LoopLiveProjectionOverlay" in source
    assert "LoopLiveRedactionPolicy" in source


def test_desktop_composition_root_does_not_reclaim_live_domain_logic() -> None:
    source = (ROOT / "desktop/src/renderer/app/app.js").read_text(encoding="utf-8")
    for forbidden in ("function startLoopStream", "function scheduleLoopRefresh", "function scheduleLoopPoll", "loopStore.apply(", "loopApi.events("):
        assert forbidden not in source
    assert "loopConnection.start(loopId).catch" in source
