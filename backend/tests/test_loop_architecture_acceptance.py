r"""本文件验证 Live Loop 的声明式文件头、职责拆分和组合根边界。

输入为新增后端/前端源文件与 app.js/live_api.py 文本；输出为头部五要素、Snapshot 传输层无领域装配依赖、app.js 无 reducer/重连/级联刷新逻辑的断言。
具体工作流为静态读取源码，不执行应用或修改文件；示例：`pytest backend/tests/test_loop_architecture_acceptance.py`。
"""

from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
AGENT_LOOP_MODULES = """
completion_policy context_run_pool curator_assignments directive_causality directive_lifecycle event_contract event_journal
fact_identity fact_lifecycle fact_materializer fact_models fact_projector fact_verification feature_flags intervention_lifecycle
journal_models fact_event_materializer portfolio_events run_activity_bridge supervisor_registry
live_access live_api live_projection_contract live_projection_projector live_projection_reducer live_routes
live_snapshot_overlay materialized_fact_query materialized_fact_sources mission_authority mission_contract mission_history
mission_models mission_repository mission_service ownership patrol_audit patrol_runtime patrol_session_models
patrol_session_repository patrol_session_state publication_queue runtime_convergence supervisor
""".split()
LOOP_TEST_MODULES = """
test_loop_architecture_acceptance test_loop_event_journal test_loop_fact_source_resilience test_loop_feature_flags
test_loop_fencing test_loop_live_acceptance test_loop_live_api_contract test_loop_live_projection
test_loop_materialized_facts test_loop_mission_contract test_loop_multi_concurrency test_loop_runtime_pools
test_loop_supervisor
""".split()
DESKTOP_MODULES = """
loop-legacy-connection loop-live-connection loop-live-projection.test loop-live-reducer loop-live-schema
loop-live-selectors loop-live-store loop-mission-editor
""".split()
SOURCE_FILES = (
    *(f"backend/app/desktop/agent_loop/{name}.py" for name in AGENT_LOOP_MODULES),
    *(f"backend/tests/{name}.py" for name in LOOP_TEST_MODULES),
    *(f"desktop/{name}.js" for name in DESKTOP_MODULES),
    "desktop/styles/loop-console.css",
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
    source = (ROOT / "backend/app/desktop/agent_loop/live_api.py").read_text(encoding="utf-8")
    for forbidden in ("LoopMissionRevision", "LoopContextMembership", "DesktopRun", "LoopFact", "PortfolioRevision"):
        assert forbidden not in source
    assert "LoopLiveProjectionOverlay" in source
    assert "LoopLiveRedactionPolicy" in source


def test_desktop_composition_root_does_not_reclaim_live_domain_logic() -> None:
    source = (ROOT / "desktop/app.js").read_text(encoding="utf-8")
    for forbidden in ("function startLoopStream", "function scheduleLoopRefresh", "function scheduleLoopPoll", "loopStore.apply(", "loopApi.events("):
        assert forbidden not in source
    assert "loopConnection?.start" in source
