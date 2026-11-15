"""Build the source-backed Focus tool atlas without importing or executing Focus.

Usage: python docs/tool-atlas/build_atlas.py [--revision GIT_REVISION]
Research JSON is the editable input. README.md and catalog.json are generated.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import subprocess
import tarfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
INPUTS = ("builtins.json", "desktop.json", "plugins.json", "extras.json")
CATEGORIES = ("工作区", "代码执行", "联网", "浏览器", "子代理", "协作", "任务板", "巡检", "上下文", "技能", "图片", "插件", "承诺流程", "外部文档适配")


def source_files(revision: str | None = None) -> dict[str, str]:
    """Read source without importing it; a revision uses Git's archive in memory."""
    roots = ("backend/packages/harness/focus", "backend/app", "plugins")
    sources = {}
    if revision:
        result = subprocess.run(["git", "archive", "--format=tar", revision, "--", *roots], cwd=ROOT, check=True, capture_output=True)
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
            for member in archive:
                if member.isfile() and member.name.endswith(".py"):
                    stream = archive.extractfile(member)
                    assert stream is not None
                    sources[member.name] = stream.read().decode("utf-8-sig")
    else:
        for root in roots:
            directory = ROOT / root
            for path in directory.rglob("*.py"):
                if any(part.startswith(".") or part in {"__pycache__", "node_modules"} for part in path.relative_to(directory).parts):
                    continue
                sources[path.relative_to(ROOT).as_posix()] = path.read_text(encoding="utf-8-sig")
    return sources


def declared_tools(sources: dict[str, str] | None = None) -> dict[str, list[str]]:
    """Find fixed tool declarations, including factories and explicit renaming."""
    found: dict[str, list[str]] = {}
    for path, source in (source_files() if sources is None else sources).items():
        tree = ast.parse(source, filename=path)
        for node in ast.walk(tree):
            name = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    target = decorator.func if isinstance(decorator, ast.Call) else decorator
                    if isinstance(target, ast.Name) and target.id == "tool":
                        name = node.name
                        if isinstance(decorator, ast.Call) and decorator.args and isinstance(decorator.args[0], ast.Constant) and isinstance(decorator.args[0].value, str):
                            name = decorator.args[0].value
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "StructuredTool" and node.func.attr == "from_function":
                name = next((keyword.value.value for keyword in node.keywords if keyword.arg == "name" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str)), None)
            if name:
                found.setdefault(name, []).append(f"{path}:{node.lineno}")
    return found


def load(sources: dict[str, str] | None = None) -> list[dict]:
    sources = source_files() if sources is None else sources
    items = []
    for filename in INPUTS:
        items.extend(json.loads((HERE / "research" / filename).read_text(encoding="utf-8-sig")))
    names, ids = set(), set()
    for item in items:
        required = {"id", "name", "category", "summary", "params", "returns", "effects", "availability", "notes", "source", "line", "example"}
        assert required <= item.keys(), f"Missing fields: {item.get('name')} {required - item.keys()}"
        assert item["name"] not in names, f"Duplicate name: {item['name']}"
        assert item["id"] not in ids, f"Duplicate id: {item['id']}"
        names.add(item["name"])
        ids.add(item["id"])
        item.setdefault("exposure", "model")
        assert item["exposure"] in {"model", "internal"}, item
        assert item["source"] in sources, f"Missing source: {item['source']}"
        lines = sources[item["source"]].splitlines()
        assert isinstance(item["line"], int) and 1 <= item["line"] <= len(lines), item
        for param in item["params"]:
            assert {"name", "type", "required", "default", "description"} <= param.keys(), param
            assert isinstance(param["required"], bool), param
        item["source_link"] = "../../" + item["source"]
    fixed = declared_tools(sources)
    assert fixed.keys() <= names, f"Uncatalogued tool declarations: {fixed.keys() - names}"
    coverage = json.loads((HERE / "research/coverage.json").read_text(encoding="utf-8-sig"))
    assert set(coverage["unique_names"]) == fixed.keys(), "Research coverage changed; refresh coverage.json"
    assert coverage["declaration_count"] == sum(map(len, fixed.values())), "Declaration count changed"
    order = {category: position for position, category in enumerate(CATEGORIES)}
    return sorted(items, key=lambda item: (order.get(item["category"], len(order)), item["name"]))


def functional_overview(items: list[dict]) -> list[str]:
    """Group the catalog by function and the three child-agent types."""
    by_name = {item["name"]: item for item in items}
    groups = [
        ("文件与目录", "按 read / write 权限装配。", [item["name"] for item in items if item["category"] == "工作区"]),
        ("命令执行", "需要 host_command 权限和对应 Shell；执行仍受访问模式及沙箱约束。", [item["name"] for item in items if item["category"] == "代码执行"]),
        ("网页搜索与抓取", "使用 DeepSeek，需要有效凭据和网络。", [item["name"] for item in items if item["category"] == "联网"]),
        ("浏览器操作", "需要 Windows 浏览器宿主和对应执行上下文；页面操作依赖有效观察。", [item["name"] for item in items if item["category"] == "浏览器"]),
        ("临时子 Agent · spawn_agent", "一次性委派，等待完成后直接取得最终回答。", ["spawn_agent"]),
        ("持久队友 · teammate", "持续负责一条工作线，可以通信、提交计划审批、申请停止，并被再次唤醒。", ["spawn_teammate", "wake_agent", "wait_for_swarm", "read_swarm_agent_history", "send_message", "request_plan_approval", "approve_plan", "request_shutdown", "respond_shutdown"]),
        ("持久工蜂 · worker", "从任务板认领明确任务，执行后提交结果；也可以通信和被再次唤醒。", ["spawn_worker", "wake_agent", "wait_for_swarm", "read_swarm_agent_history", "send_message", "publish_task", "list_board_tasks", "claim_task", "complete_task"]),
        ("巡检小兵", "查看用户投放的小兵及其已提交历史。", [item["name"] for item in items if item["category"] == "巡检"]),
        ("技能、图片与插件", "各工具有独立的条件装配规则。", [item["name"] for item in items if item["category"] in {"技能", "图片", "插件"}]),
        ("承诺流程中的模型工具", "只在对应承诺阶段向模型提供。", [item["name"] for item in items if item["category"] == "承诺流程" and item["exposure"] == "model"]),
        ("内部编排与文档适配", "由受控代码调用，不属于模型自由选择的工具目录；远端适配参数以真实 MCP schema 为准。", [item["name"] for item in items if item["exposure"] == "internal"]),
    ]
    assert {name for _, _, names in groups for name in names} == by_name.keys(), "Functional overview must cover every entry"
    callers = {
        "spawn_agent": "主 Agent（非受治理 Loop）", "spawn_teammate": "主 Agent（非受治理 Loop）",
        "spawn_worker": "主 Agent（非受治理 Loop）", "wake_agent": "主 Agent（非受治理 Loop）",
        "wait_for_swarm": "主 Agent（非受治理 Loop）；1–30 秒", "read_swarm_agent_history": "主 Agent；Loop 中仍保留",
        "send_message": "主 Agent、队友、工蜂", "request_plan_approval": "队友", "approve_plan": "主 Agent",
        "request_shutdown": "队友", "respond_shutdown": "主 Agent", "publish_task": "主 Agent",
        "list_board_tasks": "主 Agent", "claim_task": "工蜂", "complete_task": "认领该任务的工蜂",
        "list_patrol_agents": "主 Agent", "read_patrol_agent_history": "主 Agent", "describe_skill": "主 Agent 等按条件装配",
        "demo_echo": "演示插件启用并加载成功", "report_must_view_images": "主 Agent 当前轮存在必看图片",
        "find_reference_urls": "承诺阶段 4 Worker", "delegate_with_review": "内部 ToolNode",
        "resolve-library-id": "承诺阶段 5，Python 调用", "query-docs": "承诺阶段 5／6，Python 调用",
        "read_file": "read", "discover_files": "read", "write_file": "write", "edit_file": "read + write",
    }
    output = ["## 功能图鉴", "", "按功能分表；子 Agent 工具按临时子 Agent、持久队友、持久工蜂分类。共享工具重复列出但不重复计数。点击工具名可查看完整参数、权限、输出、示例和源码。", ""]
    for heading, description, names in groups:
        output.extend([f"### {heading}", "", description, "", "| 工具 | 功能 | 主要参数 | 调用方／条件 |", "| --- | --- | --- | --- |"])
        for name in names:
            item = by_name[name]
            required = [param["name"] for param in item["params"] if param["required"]]
            optional = [param["name"] for param in item["params"] if not param["required"]]
            params = required + optional[:max(0, 4 - len(required))]
            arguments = "、".join(f"`{param}`" for param in params) or "无"
            fallback = {"代码执行": "host_command + 对应 Shell", "联网": "DeepSeek 凭据与网络", "浏览器": "Windows 浏览器执行上下文"}.get(item["category"], "按条件装配")
            output.append(f"| [`{name}`](#{name}) | {item['summary']} | {arguments} | {callers.get(name, fallback)} |")
        output.append("")
    return output


def markdown(items: list[dict], revision: str | None = None) -> str:
    model = sum(item["exposure"] == "model" for item in items)
    adapters = sum(item["exposure"] == "internal" and item["category"] == "外部文档适配" for item in items)
    orchestration = len(items) - model - adapters
    text = [
        "# Focus 工具图鉴", "",
        f"调查日期：2026-11-15。可静态枚举 **{model} 个模型工具**，另收录 **{orchestration} 个内部编排工具、{adapters} 个内部远端适配条目**。这是源码覆盖清单，不表示任一 Run 同时拥有全部工具。MCP、自定义工具、用户插件和进程外插件为动态来源，数量未知，未并入静态总数。", "",
        f"源码依据：{'Git 提交 `' + revision + '`' if revision else '当前工作区文件'}。四个子 agent 分别调查内置工具、桌面与协作工具、插件、自定义/MCP 与装配架构；没有调用业务工具、连接外部 MCP 或读取用户私有配置。", "",
        "工具调用示例是参数说明，不是执行记录。`runtime` 与服务端执行身份不属于模型可见参数；声明可见、图中绑定、执行获准是不同条件。", "",
        *functional_overview(items),
        "## 工具怎样进入 Agent", "", "```mermaid", "flowchart TD",
        '  A["内置 workspace / web"] --> D["发现工具池"]',
        '  B["用户自定义工具 / MCP"] --> D',
        '  D --> E["按角色与权限选择常规工具"]',
        '  F["桌面浏览器 / 协作 / 工厂工具"] --> E',
        '  P["启用的插件"] --> M["PluginBridgeMiddleware"]',
        '  E --> C["联合目录编译：拒绝同名工具"]',
        '  M --> C',
        '  C --> S["模型 Schema 与执行分发同一快照"]',
        '  S --> G["执行身份 / AccessPolicy / 效果契约准入"]',
        '  G --> R["实际调用与结果"]', "```", "",
        "完整的角色装配、MCP 信任边界和内部协议说明见 [架构调查](research/architecture.md)；插件与 ONLYOFFICE 边界见 [插件调查](research/plugin-notes.md)。", "",
    ]
    text.extend(["", "## 调用条件速览", "",
        "- 工作区工具按 `read`、`write`、`host_command` 选择，并受 AccessMode 和效果契约准入约束；四个 Shell 是独立工具名。",
        "- 浏览器、主代理协作、队友审批与工蜂任务板按角色装配；普通 Main、Context 执行和独立 Patrol 的工具组合不同。",
        "- `describe_skill`、图片回执与临时子代理工具另有条件；插件桥仍可能向这些图贡献启用插件，不能把普通工具列表当成最终全集。",
        "- `config.yaml` 的声明式 `tool_groups` 不再决定当前工具清单。动态 MCP 与 custom 的工具名、参数及可用性必须以实际 Run 的冻结目录为准。",
        "- ONLYOFFICE HTTP/前端桥、插件 Hook、Patrol 的 `reads`/`decision` 等 JSON 协议能力不是独立 Agent tool。",
        "- `no_local_effect` 仅描述本地文件系统效果，不表示没有联网、数据库写入、协作消息或浏览器状态变化。",
        "", "## 逐项图鉴", ""])
    for item in items:
        label = "内部远端适配" if item["category"] == "外部文档适配" else ("内部编排" if item["exposure"] == "internal" else "模型工具")
        text.extend([f"### {item['name']}", "", f"**{item['category']} · {label}** — {item['summary']}", ""])
        if item["params"]:
            text.extend(["| 参数 | 类型 | 要求 | 默认值 | 含义 |", "| --- | --- | --- | --- | --- |"])
            for param in item["params"]:
                values = [f"`{param['name']}`", param["type"], "必填" if param["required"] else "可选", "—" if param["required"] else json.dumps(param["default"], ensure_ascii=False), param["description"]]
                text.append("| " + " | ".join(str(value).replace("|", "\\|").replace("\n", "<br>") for value in values) + " |")
            text.append("")
        else:
            text.extend(["无模型可见参数。", ""])
        text.extend([f"**返回：** {item['returns']}", "", f"**启用条件：** {item['availability']}", "", f"**效果与权限：** {item['effects']}", ""])
        notes = item["notes"] if isinstance(item["notes"], list) else [item["notes"]]
        text.extend(f"- {note}" for note in notes if note)
        if notes:
            text.append("")
        example = item["example"] if isinstance(item["example"], str) else json.dumps(item["example"], ensure_ascii=False, indent=2)
        text.extend(["```text", example, "```", "", f"源码：[{item['source']}:{item['line']}]({item['source_link']}#L{item['line']})", ""])
    text.extend(["## 维护与核验", "", "输入为 `research/*.json`，生成器不导入 Focus，不启动插件或 MCP。", "", "```powershell", "python docs/tool-atlas/build_atlas.py", "```", "", "生成器校验名称唯一、参数结构、源码文件与行号，并用 AST 扫描 harness、desktop、plugins 的 `@tool` 和具名 `StructuredTool.from_function` 固定声明，要求全部被收录。动态注册点、装配语义及同名包装仍由研究记录解释；静态检查不能证明运行环境可用。", "", "结构化目录：[catalog.json](catalog.json)。研究输入与边界说明位于 [research](research/)。", ""])
    return "\n".join(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", help="Validate against a committed Git source snapshot instead of the working directory")
    args = parser.parse_args()
    sources = source_files(args.revision)
    items = load(sources)
    (HERE / "catalog.json").write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (HERE / "README.md").write_text(markdown(items, args.revision), encoding="utf-8")
    print(json.dumps({"entries": len(items), "model_tools": sum(item["exposure"] == "model" for item in items), "internal_tools": sum(item["exposure"] == "internal" for item in items), "fixed_declaration_names": len(declared_tools(sources)), "categories": dict(Counter(item["category"] for item in items))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
