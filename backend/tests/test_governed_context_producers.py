"""本文件对外提供"受治理运行上下文键的生产者契约"检查。

输入为受治理键注册表、启动点源码与组装入口；输出为五类断言结果：声明与生产者一一对应（缺失即失败）、
调用方载荷不能成为受治理键的来源、组装入口产出全部受治理键、任务身份在各执行形态下一致、缺键时逐键
显式失败；另有两条静态守卫：启动点的 extras 不得夹带受治理键、消费者用例不得手写运行上下文。

工作流只读取注册表与源码并调用既有纯函数，不修改运行时代码。示例：
`python -m pytest backend/tests/test_governed_context_producers.py`。
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

import backend.app.desktop.collab  # noqa: F401  受治理键与生产者的声明消费者
import backend.app.desktop.service  # noqa: F401
import focus.agents.commitment.middleware  # noqa: F401
import focus.agents.material_inputs  # noqa: F401
import focus.agents.must_view  # noqa: F401
import focus.security.context  # noqa: F401
import focus.security.launch  # noqa: F401
import focus.security.policy  # noqa: F401
import plugins.spatial_patrol.spatial_context  # noqa: F401
from focus.security.context import (
    AuthorizationIdentity,
    ExecutionProfile,
    RoutingIdentity,
    security_context_of,
)
from focus.security.governed import (
    declare_governed_producer,
    governed_keys,
    governed_producers,
    missing_producers,
)
from focus.security.launch import assemble_run_context
from focus.security.policy import AccessMode

REPO = Path(__file__).resolve().parents[2]

# 声明为受治理、但按设计不由运行上下文承载的键：它们的作用是阻止调用方伪造（剥除），
# 而不是从上下文读取——因此没有生产者。新增的任何未生产键都会让下面的相等断言失败。
GOVERNED_WITHOUT_PRODUCER = {
    "allow_global_config": "只在没有安全上下文的回退分支被读，缺省即最严；声明用于阻止调用方伪造工作根",
    "must_view_materials": "已被 run_image_inputs 取代，仅作 legacy 读取兜底",
}

# 条件投影：只在执行身份档案带有该字段时才出现（模型名可选、归属在单用户桌面下常缺省）；
# 读取点对这两个键都按"可缺省"处理，因此不纳入"必须始终出现"的集合。
CONDITIONAL_CONTEXT_KEYS = {
    "user_id": "仅当执行有归属时投影",
    "model_name": "仅当执行身份带模型名时投影",
}


def _profile(task_id: str = "t-1", agent_id: str = "main:t-1") -> ExecutionProfile:
    workspace = Path("C:/ws")
    return ExecutionProfile(
        authorization=AuthorizationIdentity(
            workspace=workspace,
            roots=(workspace,),
            permissions=("read",),
            access_mode=AccessMode.WORKSPACE,
            agent_role="main",
        ),
        routing=RoutingIdentity(
            thread_id="thread-1",
            workspace_id="ws-1",
            agent_id=agent_id,
            task_id=task_id,
            checkpoint_ns="",
            run_id="run-1",
        ),
    )


def test_every_governed_key_has_a_producer_or_a_recorded_exemption():
    """声明与生产者一一对应；未生产的键必须逐条有记录在案的理由。"""
    assert missing_producers() == frozenset(GOVERNED_WITHOUT_PRODUCER)
    for key, reason in GOVERNED_WITHOUT_PRODUCER.items():
        assert reason.strip(), f"未生产的受治理键 {key} 缺少理由"


def test_producers_are_declared_for_the_recovered_keys():
    """回归修复的三个键：任务身份由身份投影生产，唤醒链深度由执行提示生产，上传清单不再受治理。"""
    producers = governed_producers()
    assert producers["task_id"] == "focus.security.context.to_runtime_context"
    assert producers["swarm_depth"] == "focus.security.launch.project_dispatch_hints"
    assert "uploads" not in governed_keys()
    assert "run_image_inputs" in producers and "model_supports_image_input" in producers


def test_a_second_producer_for_the_same_key_is_rejected():
    """受治理键只允许一个来源：第二个生产者必须显式冲突，而不是靠导入顺序决定谁赢。"""
    with pytest.raises(RuntimeError, match="第二个生产者"):
        declare_governed_producer("swarm_depth", "somewhere.else")


def test_caller_payload_cannot_supply_a_governed_key():
    """调用方载荷里的受治理键被剥除：读到的值只来自服务端生产者。"""
    context = assemble_run_context(
        _profile(task_id="real-task"),
        {"task_id": "forged-task", "workspace_id": "forged-ws", "skills": ["a"]},
    )
    assert context["task_id"] == "real-task"
    assert context["workspace_id"] == "ws-1"
    assert context["skills"] == ["a"]


ASSEMBLY_PRODUCERS = frozenset({
    "focus.security.context.to_runtime_context",
    "focus.security.launch.project_dispatch_hints",
})
"""由统一组装入口负责生产的受治理键的生产者标识；其余键各有自己的生产者写回点。"""


def test_assembly_produces_the_keys_it_owns():
    """统一组装入口必须产出它负责的全部受治理键（身份投影 + 执行提示），不多不少。"""
    owned = {
        key for key, producer in governed_producers().items() if producer in ASSEMBLY_PRODUCERS
    } - set(CONDITIONAL_CONTEXT_KEYS)
    assert owned, "组装入口未声明任何生产者"
    for hints in ({"swarm_depth": 0}, {"swarm_depth": 2}):
        context = assemble_run_context(_profile(), {"skills": []}, dispatch_hints=hints)
        assert owned <= set(context), f"缺键: {sorted(owned - set(context))}"
        assert context["swarm_depth"] == hints["swarm_depth"]


def test_material_producer_writes_the_keys_it_declares():
    """材料投影生产者必须真的写出它声明生产的键。"""
    from focus.agents.image_inputs import MODEL_IMAGE_INPUT_KEY, RUN_IMAGE_INPUTS_CONTEXT_KEY
    from focus.agents.material_inputs import RunMaterialInputs, project_run_material_context

    inputs = RunMaterialInputs.build(
        "run-1", "msg-1",
        [{
            "material_id": "m1", "relative_path": "notes.md", "digest": "d1",
            "material_kind": "text", "size_bytes": 10,
        }],
    )
    context = project_run_material_context({}, inputs, True)
    declared = {
        key for key, producer in governed_producers().items()
        if producer == "focus.agents.material_inputs.project_run_material_context"
    }
    assert declared == {RUN_IMAGE_INPUTS_CONTEXT_KEY, MODEL_IMAGE_INPUT_KEY}
    assert declared <= set(context)
    assert inputs.uploads_tag == "<current_uploads>\nnotes.md\n</current_uploads>"


def test_spatial_producer_writes_the_keys_it_declares():
    """空间上下文生产者必须真的写出它声明生产的键（插件启动点与用例共用它）。"""
    from plugins.spatial_patrol.spatial_context import SPATIAL_CONTEXT_KEYS, project_spatial_context

    declared = {
        key for key, producer in governed_producers().items()
        if producer == "plugins.spatial_patrol.spatial_context.project_spatial_context"
    }
    assert declared == set(SPATIAL_CONTEXT_KEYS)
    anchor = SimpleNamespace(
        spatial_id="spatial-1", task_id="t-1", content_ref="sample.docx", page=1, x=0.5, y=3.0,
    )
    context = project_spatial_context({}, anchor, change_evidence={"changed": True})
    assert declared <= set(context)
    with pytest.raises(RuntimeError, match="空间锚点缺少字段"):
        project_spatial_context({}, SimpleNamespace(spatial_id="s", content_ref="c", page=1, x=0.5))


def test_launch_shapes_cover_every_governed_key_together():
    """每种启动形态覆盖自己负责的受治理键，且各形态并集覆盖全部非豁免键。"""
    from focus.agents.material_inputs import RunMaterialInputs, project_run_material_context
    from plugins.spatial_patrol.spatial_context import project_spatial_context

    materials = RunMaterialInputs.build(
        "run-1", "msg-1",
        [{
            "material_id": "m1", "relative_path": "notes.docx", "digest": "d1",
            "material_kind": "text", "size_bytes": 10,
        }],
    )
    desktop = assemble_run_context(_profile(), {"skills": []}, dispatch_hints={"swarm_depth": 1})
    project_run_material_context(desktop, materials, True)

    anchor = SimpleNamespace(
        spatial_id="spatial-1", task_id="t-1", content_ref="notes.docx", page=1, x=0.5, y=3.0,
    )
    spatial = assemble_run_context(
        _profile(agent_id="spatial-1"), {}, dispatch_hints={"swarm_depth": 0}
    )
    project_spatial_context(spatial, anchor)

    spatial_keys = {
        key for key, producer in governed_producers().items()
        if producer == "plugins.spatial_patrol.spatial_context.project_spatial_context"
    }
    material_keys = {
        key for key, producer in governed_producers().items()
        if producer == "focus.agents.material_inputs.project_run_material_context"
    }
    assert material_keys <= set(desktop), f"桌面形态缺: {sorted(material_keys - set(desktop))}"
    assert spatial_keys <= set(spatial), f"空间形态缺: {sorted(spatial_keys - set(spatial))}"

    required = governed_keys() - set(GOVERNED_WITHOUT_PRODUCER) - set(CONDITIONAL_CONTEXT_KEYS)
    covered = set(desktop) | set(spatial)
    assert required <= covered, f"没有任何启动形态产出的受治理键: {sorted(required - covered)}"


def test_governed_keys_are_never_read_with_a_default_value():
    """静态守卫：运行上下文里的受治理键不得以"取不到就用默认值"的读法出现——那是静默降级的形态。

    判据限定在运行上下文的接收者（context / runtime.context / langgraph_context / runtime_context）上，
    因为 `permissions` 这类同名键在装备快照（equipment）里是普通载荷，不属于运行上下文。
    """
    import re

    receivers = r"(?:runtime[_.]?context|langgraph_context|runtime_context|\bcontext)"
    offenders: list[str] = []
    roots = [REPO / "backend" / "app", REPO / "backend" / "packages" / "focus", REPO / "plugins"]
    files = [path for root in roots for path in root.rglob("*.py") if "__pycache__" not in path.parts]
    patterns = [
        (key, re.compile(receivers + r"\.get\(\s*[\"']" + re.escape(key) + r"[\"']\s*,"))
        for key in governed_keys()
    ]
    for path in files:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for key, pattern in patterns:
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(REPO)}:{lineno} {key}")
    assert offenders == [], f"运行上下文里受治理键的带默认值读法: {offenders}"


def test_routing_identity_without_task_identity_cannot_be_constructed():
    """任务身份是执行身份的一部分：漏填的启动点必须构造即失败，而不是把空值带进运行。"""
    with pytest.raises(TypeError):
        RoutingIdentity(
            thread_id="thread-1", workspace_id="ws-1", agent_id="main:t-1",
            checkpoint_ns="", run_id="run-1",
        )


def test_commitment_uploads_come_from_the_material_projection():
    """承诺层看到的上传清单与本轮绑定材料一致，来源是 run_material_inputs。"""
    from focus.agents.commitment.middleware import _uploads_tag
    from focus.agents.material_inputs import RunMaterialInputs
    from backend.tests.runtime_context_support import runtime_context

    materials = RunMaterialInputs.build(
        "run-1", "msg-1",
        [
            {"material_id": "m1", "relative_path": "notes.md", "digest": "d1",
             "material_kind": "text", "size_bytes": 10},
            {"material_id": "m2", "relative_path": "shot.png", "digest": "d2",
             "material_kind": "image", "size_bytes": 20},
        ],
    )
    bound = runtime_context(agent_id="main:t-1", task_id="t-1", materials=materials)
    assert _uploads_tag(bound) == materials.uploads_tag
    assert "notes.md" in _uploads_tag(bound) and "shot.png" in _uploads_tag(bound)
    assert _uploads_tag(runtime_context(agent_id="main:t-1", task_id="t-1")) == ""


def test_task_identity_is_identical_across_execution_shapes():
    """主 run 与派生的 Agent run 读到同一个任务身份，且都等于它们所属会话的任务标识。"""
    main = assemble_run_context(_profile(), {}, dispatch_hints={"swarm_depth": 0})
    swarm = assemble_run_context(
        _profile(agent_id="swarm-abc"), {}, dispatch_hints={"swarm_depth": 3}
    )
    assert main["task_id"] == swarm["task_id"] == "t-1"
    assert security_context_of(main).routing.task_id == "t-1"
    assert security_context_of(swarm).routing.task_id == "t-1"


def test_missing_governed_key_fails_with_the_key_name():
    """缺键逐键失败：失败信息只提缺失的那个键，且零值不被当成缺失。"""
    from backend.app.desktop.collab import _collab_values

    class _Runtime:
        def __init__(self, context):
            self.context = context

    with pytest.raises(RuntimeError, match=r"\['task_id'\]"):
        _collab_values(_Runtime({"agent_id": "main:t-1"}))
    with pytest.raises(RuntimeError, match=r"\['agent_id'\]"):
        _collab_values(_Runtime({"task_id": "t-1"}))

    runtime = _Runtime(
        {"agent_id": "main:t-1", "task_id": "t-1", "swarm_depth": 0, "workspace": "C:/ws"}
    )
    assert _collab_values(runtime) == ("main:t-1", "t-1")
    from backend.app.desktop.collab import _context_value

    assert _context_value(runtime.context, "swarm_depth", "唤醒链深度") == "0"


def _call_keywords(path: Path, names: set[str]) -> list[tuple[int, ast.Call]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, ast.Call]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if target in names:
            found.append((node.lineno, node))
    return found


def test_launch_extras_never_carry_governed_keys():
    """静态守卫：启动点的 extras 不得夹带受治理键——一旦夹带就会被剥除，这正是本次回归的形态。"""
    path = REPO / "backend" / "app" / "desktop" / "service.py"
    calls = _call_keywords(path, {"_governed_context", "assemble_run_context"})
    assert calls, "未找到启动上下文组装调用点"
    for lineno, node in calls:
        for keyword in node.keywords:
            if keyword.arg != "extras" or not isinstance(keyword.value, ast.Dict):
                continue
            for key in keyword.value.keys:
                assert not (
                    isinstance(key, ast.Constant) and key.value in governed_keys()
                ), f"service.py:{lineno} 的 extras 夹带了受治理键 {getattr(key, 'value', key)!r}"


def test_consumer_tests_do_not_hand_build_governed_context_keys():
    """静态守卫：消费者用例不得手写受治理的运行上下文键——那会测出生产路径产不出的形状。

    豁免两类：按设计只从上下文读取（阻止伪造）的键，以及该文件故意构造"缺少安全上下文"的降级上下文。
    """
    allowed = set(GOVERNED_WITHOUT_PRODUCER)
    exempt_files = {
        "test_security_context.py": "该文件故意构造缺少安全上下文的降级上下文，用于断言缺失即失败",
    }
    offenders: list[str] = []
    for path in sorted((REPO / "backend" / "tests").glob("test_*.py")):
        if path.name in exempt_files:
            continue
        for lineno, node in _call_keywords(path, {"ToolRuntime"}):
            for keyword in node.keywords:
                if keyword.arg != "context" or not isinstance(keyword.value, ast.Dict):
                    continue
                for key in keyword.value.keys:
                    if isinstance(key, ast.Constant) and key.value in governed_keys() - allowed:
                        offenders.append(f"{path.name}:{lineno} 手写了 {key.value!r}")
    assert offenders == [], f"手写受治理键的用例: {offenders}"
