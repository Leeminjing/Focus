r"""本文件对外提供 Context-Governed Agent Loop 模块边界与声明式文件头静态测试。

输入为新增后端领域模块和 Desktop 入口源码；输出为文件头完整性与依赖方向断言。
具体工作流为扫描每个约定文件、解析 Python import，并拒绝缺失说明或越层依赖。
示例：`python -m pytest backend/tests/test_agent_loop_architecture.py -q`。
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[2]
BACKEND_ROOT = ROOT / "backend" / "app" / "desktop"
DESKTOP_ROOT = ROOT / "desktop"
BACKEND_PACKAGES = {
    "context_evolution": set(),
    "workspace_coordination": set(),
    "run_orchestration": {"context_evolution", "workspace_coordination"},
    "context_curation": {"context_evolution"},
    "agent_loop": {
        "context_evolution",
        "workspace_coordination",
        "run_orchestration",
        "context_curation",
    },
}
DESKTOP_MODULES = (
    "loop-api.js",
    "loop-store.js",
    "loop-view.js",
    "portfolio-view.js",
    "context-evolution-view.js",
    "message-provenance-view.js",
    "workspace-slots-view.js",
)
HEADER_MARKERS = ("本文件对外提供", "输入为", "输出为", "具体工作流为", "示例")


def _python_files() -> list[Path]:
    return [
        path
        for package in BACKEND_PACKAGES
        for path in (BACKEND_ROOT / package).glob("*.py")
    ]


def _imported_domain(module: str | None) -> str | None:
    prefix = "backend.app.desktop."
    if not module or not module.startswith(prefix):
        return None
    candidate = module.removeprefix(prefix).split(".", 1)[0]
    return candidate if candidate in BACKEND_PACKAGES else None


def test_new_domain_files_have_declarative_headers() -> None:
    paths = _python_files() + [DESKTOP_ROOT / name for name in DESKTOP_MODULES]
    assert paths
    for path in paths:
        source = path.read_text(encoding="utf-8")
        header = source[:1200]
        missing = [marker for marker in HEADER_MARKERS if marker not in header]
        assert not missing, f"{path.relative_to(ROOT)} 文件头缺少 {missing}"


def test_new_backend_domains_follow_dependency_direction() -> None:
    violations: list[str] = []
    for source_package, allowed in BACKEND_PACKAGES.items():
        for path in (BACKEND_ROOT / source_package).glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.ImportFrom):
                    modules = [node.module]
                elif isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                for module in modules:
                    target = _imported_domain(module)
                    if target and target != source_package and target not in allowed:
                        violations.append(f"{path.name}: {source_package} -> {target}")
    assert not violations, "非法领域依赖：" + ", ".join(sorted(violations))
