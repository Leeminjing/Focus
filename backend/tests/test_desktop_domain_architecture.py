"""本文件验证桌面领域组织和组合根的依赖约束。

输入为桌面 Python 模块与独立解释器；输出为导入可解析、请求模型不注册 ORM、
领域不反向依赖组合根及入口仅转发业务的断言。具体工作流为解析依赖并检查
独立进程的请求合同导入。示例：python -m pytest backend/tests/test_desktop_domain_architecture.py。
"""

import ast
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "backend/app/desktop"


def test_desktop_root_keeps_only_registration_entry_points():
    assert {path.name for path in DESKTOP.glob("*.py")} == {"__init__.py", "app.py"}


def test_all_desktop_module_dependencies_resolve():
    missing = []
    for path in DESKTOP.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                modules = [node.module] if node.module else []
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            else:
                continue
            for module in modules:
                if not module.startswith("backend.app.desktop"):
                    continue
                target = ROOT.joinpath(*module.split("."))
                if (
                    not target.with_suffix(".py").is_file()
                    and not (target / "__init__.py").is_file()
                ):
                    missing.append((path.relative_to(DESKTOP).as_posix(), module))
    assert missing == []


def test_request_contract_import_does_not_register_database_models():
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(ROOT), str(ROOT / "backend/packages/harness"))
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import backend.app.desktop.api.schemas; "
            "assert 'focus.persistence.base' not in sys.modules; "
            "assert 'backend.app.desktop.persistence.models' not in sys.modules",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_domains_do_not_import_the_composition_root():
    offenders = []
    for path in DESKTOP.rglob("*.py"):
        if (
            path.relative_to(DESKTOP).parts[0] in {"api", "composition"}
            or path.name == "app.py"
        ):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules = (
                ([node.module] if node.module else [])
                if isinstance(node, ast.ImportFrom)
                else (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else []
                )
            )
            if any(
                module.startswith("backend.app.desktop.composition")
                for module in modules
            ):
                offenders.append(path.relative_to(DESKTOP).as_posix())
    assert offenders == []


def test_composition_facade_only_owns_wiring_and_runtime_lifecycle():
    tree = ast.parse((DESKTOP / "composition/service.py").read_text(encoding="utf-8"))
    service = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DesktopService"
    )
    lifecycle = {
        "__init__",
        "start",
        "close",
        "notify_run_dispatch",
        "_run_dispatch_loop",
    }
    for method in service.body:
        if (
            not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
            or method.name in lifecycle
        ):
            continue
        assert len(method.body) == 1 and isinstance(method.body[0], ast.Return), (
            method.name
        )
    assert not any(
        isinstance(node, ast.Name) and node.id == "select" for node in ast.walk(tree)
    )


def test_domain_services_keep_internal_calls_out_of_dependency_ports():
    for path in DESKTOP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for domain in (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and not node.name.endswith("Ports")
        ):
            methods = {
                node.name
                for node in domain.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            for node in ast.walk(domain):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "ports"
                ):
                    assert node.attr not in methods, (path.name, domain.name, node.attr)
