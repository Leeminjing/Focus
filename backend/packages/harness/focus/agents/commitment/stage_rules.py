"""
本文件对外提供九阶段指令、结果校验、路径片段校验和 Context7 结果解析函数。

输入:
    当前阶段编号、Supervisor messages、WorkerOutput 及 Context7 工具返回内容。

输出:
    TaskEnvelope — 根据阶段规则生成的下一阶段任务信封。
    str | None — 阶段结果的校验错误；None 表示结构与前置约束通过。
    规范化结果 — 优先级、版本证据、冲突状态和参考输入判断结果。

具体工作流:
    (1) 根据阶段编号选择指令、验收条件和执行时限。
    (2) 解析模型或 Context7 返回的结构化内容。
    (3) 从 Supervisor ToolMessage 读取前序结果并校验阶段一致性。
    (4) 输出 Supervisor 决定推进、重试或人工介入所需的确定性判断。

示例:
    error = _validate_stage_result(3, result, envelope.context)
"""

import json
import re
from typing import Any

from langchain.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from focus.agents.commitment.schemas import (
    ReviewOutput,
    StageFiveResult,
    StageFourResult,
    StageTwoResult,
    TaskEnvelope,
    WorkerOutput,
)

_HUMAN_REVIEW_STAGES = frozenset({3, 5, 6, 7})

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")

_MAX_REVIEW_ATTEMPTS = 3

_STAGE_TIMEOUT_SECONDS = 600

_KNOWLEDGE_STAGE_TIMEOUT_SECONDS = 900

_LATEST_STABLE_VERSION = "latest-stable"

_STAGE_INSTRUCTIONS: dict[int, tuple[str, list[str]]] = {
    1: ("明确用户的单一主目标，保留边界和预期结果。", ["目标清晰", "不引入用户未提出的目标"]),
    2: (
        "汇总全部要求并核验技术兼容性。逐项识别应用类型、UI载体、运行平台和宿主模型；"
        "Web、桌面、移动、CMS模块与独立应用不得因同属一种语言或运行时而判为兼容。"
        "本阶段不识别精确版本，不得仅因用户未提供版本号而标记unresolved；版本留到阶段5。",
        [
            "要求完整",
            "返回requirements、compatibility_checks和conflicts",
            "每项技术有verified、conflict或unresolved状态",
            "冲突或无法核实的组合不得写成兼容",
        ],
    ),
    3: (
        "基于第二步已经明确且无未决矛盾的要求集合，给每条要求分配1、2、3三档优先级；"
        "3=必须，2=可协商，1=可选。",
        [
            "每条要求有等级且只使用1到3",
            "不得重新解释或夹带第二步的矛盾处理",
        ],
    ),
    4: (
        "解析用户引用的文件与网址。文件必须与<current_uploads>中的精确文件名核对；"
        "文件名不完整时列出上传文件候选并等待人工确认。用户提到网站、文档或项目但未给URL时，"
        "必须先调用find_reference_urls查找候选URL，再等待人工确认。",
        [
            "返回files和urls",
            "完整文件名标记matched，简称候选标记proposed，找不到标记unresolved",
            "用户给出的完整URL标记provided，搜索候选标记proposed，找不到标记unresolved",
            "不得把未确认候选写成已确认输入",
        ],
    ),
    5: ("识别涉及技术，对比项目当前版本与 Context7 候选最新稳定版。", ["每项技术有精确版本或latest-stable策略", "不得猜测版本"]),
    6: ("按已批准版本调用 Context7 获取官方技术知识。", ["每项知识含技术、版本、官方来源和正文", "不得使用非官方来源"]),
    7: (
        "把已批准的阶段结果组装为完整 Markdown 任务合同。",
        [
            "返回result.contract_markdown",
            "合同包含九步已有结论",
            "合同可直接指导执行",
        ],
    ),
    8: ("产出最终 task_contract。", ["内容与磁盘合同一致"]),
    9: ("准备 lead agent 的最终合同消息。", ["只包含合同和理论基础"]),
}


def _stage_timeout(stage: int) -> int:
    return (
        _KNOWLEDGE_STAGE_TIMEOUT_SECONDS
        if stage in {5, 6}
        else _STAGE_TIMEOUT_SECONDS
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _safe_segment(value: str, label: str) -> str:
    if value in {".", ".."} or not value or not _SAFE_SEGMENT.fullmatch(value):
        raise ValueError(f"{label} 只允许字母、数字、点、下划线和连字符")
    return value


def _slug_segment(value: str, label: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return _safe_segment(slug, label)


def _stage_envelope(stage: int, state: dict[str, Any], feedback: str = "") -> TaskEnvelope:
    instruction, criteria = _STAGE_INSTRUCTIONS[stage]
    context: dict[str, Any] = {}
    if source_text := state.get("source_text"):
        context["source_text"] = source_text
    if uploads_tag := state.get("uploads_tag"):
        context["current_uploads"] = uploads_tag
    if feedback:
        context["human_feedback"] = feedback
    return TaskEnvelope(
        stage=stage,
        instruction=instruction,
        context=context,
        acceptance_criteria=criteria,
    )


def _extract_structured(result: dict[str, Any], schema: type[BaseModel]) -> BaseModel:
    value = result.get("structured_response")
    if isinstance(value, schema):
        return value
    if value is not None:
        return schema.model_validate(value)
    for message in reversed(result.get("messages", [])):
        if not isinstance(message, AIMessage):
            continue
        content = message.content
        if isinstance(content, list):
            content = "\n".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict)
            )
        text = str(content).strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)
        try:
            return schema.model_validate_json(text)
        except ValueError:
            if schema is not ReviewOutput:
                raise
            approved = re.search(
                r'"approved"\s*:\s*(true|false)',
                text,
                re.IGNORECASE,
            )
            feedback = re.search(
                r'"feedback"\s*:\s*"(.*)"\s*}\s*$',
                text,
                re.DOTALL,
            )
            if not approved or not feedback:
                raise
            return ReviewOutput(
                approved=approved.group(1).lower() == "true",
                feedback=feedback.group(1),
            )
    raise ValueError(f"模型未返回 {schema.__name__} JSON")


def _context7_text(result: Any) -> str:
    if isinstance(result, list):
        return "\n".join(
            str(item.get("text", ""))
            for item in result
            if isinstance(item, dict)
        )
    return str(result)


def _context7_library_id(result: Any) -> str | None:
    match = re.search(
        r"Context7-compatible library ID:\s*(/\S+)",
        _context7_text(result),
    )
    return match.group(1).strip() if match else None


def _context7_stable_version(result: Any) -> str | None:
    text = _context7_text(result)
    patterns = (
        r"latest stable version(?:\s+of\s+[^.\n]+)?\s+(?:is|as)\s+",
        r"latest stable version\s*:\s*",
        r"latest version\s*:\s*",
        r"current latest version(?:\s+of\s+[^.\n]+)?\s+is\s+",
    )
    version_pattern = r"`?[vV]?(\d+\.\d+(?:\.\d+)?)`?(?![-0-9A-Za-z])"
    for prefix in patterns:
        match = re.search(prefix + version_pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _context7_candidate_version(
    result: Any,
    library_id: str,
) -> str | None:
    text = _context7_text(result)
    block = next(
        (
            item
            for item in re.split(r"\n-+\n", text)
            if f"Context7-compatible library ID: {library_id}" in item
        ),
        "",
    )
    versions = re.search(
        r"^\s*-?\s*Versions:\s*(.+)$",
        block,
        re.MULTILINE,
    )
    if not versions:
        return None
    candidates = re.findall(
        r"(?<![0-9A-Za-z])v?(\d+\.\d+(?:\.\d+)?)(?![-0-9A-Za-z.])",
        versions.group(1),
    )
    return max(
        candidates,
        key=lambda value: tuple(int(part) for part in value.split(".")),
        default=None,
    )


def _context7_version_evidence(
    result: Any,
    version: str,
) -> str | None:
    for line in _context7_text(result).splitlines():
        if version in line and (
            "version" in line.lower()
            or "latest stable" in line.lower()
        ):
            return line.strip()[:1000]
    return None


def _normalize_stage_three_result(
    output: WorkerOutput,
    requirements: list[str],
) -> WorkerOutput:
    assignments = output.result.get("priority_assignments")
    if isinstance(assignments, list):
        expected_ids = [f"R{index}" for index in range(1, len(requirements) + 1)]
        if len(assignments) != len(expected_ids):
            return output
        by_id: dict[str, int] = {}
        for item in assignments:
            if not isinstance(item, dict):
                return output
            requirement_id = item.get("requirement_id")
            priority = item.get("priority")
            if (
                requirement_id not in expected_ids
                or requirement_id in by_id
                or type(priority) is not int
                or priority not in {1, 2, 3}
            ):
                return output
            by_id[requirement_id] = priority
        raw_items = [
            {
                "requirement": requirement,
                "priority": by_id[requirement_id],
            }
            for requirement_id, requirement in zip(
                expected_ids,
                requirements,
                strict=True,
            )
        ]
    else:
        raw_items = output.result.get("requirements")
    if not isinstance(raw_items, list) or len(raw_items) != len(requirements):
        return output
    if any(
        not isinstance(item, dict)
        or item.get("requirement") != requirements[index]
        or type(item.get("priority")) is not int
        or item["priority"] not in {1, 2, 3}
        for index, item in enumerate(raw_items)
    ):
        return output

    priority_map = "、".join(
        f"R{index}={item['priority']}"
        for index, item in enumerate(raw_items, start=1)
    )
    return output.model_copy(
        update={
            "result": {"requirements": raw_items},
            "reasoning_summary": (
                f"阶段3已逐项、逐字沿用阶段2的{len(raw_items)}条保留要求并首次分配优先级："
                f"{priority_map}。已放弃要求未进入等级列表；"
                "result.requirements 是最终等级的唯一事实来源。"
            )
        }
    )


def _message_payload(message: BaseMessage) -> dict[str, Any] | None:
    if not isinstance(message, ToolMessage):
        return None
    try:
        value = json.loads(str(message.content))
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _stage_result(messages: list[BaseMessage], stage: int) -> dict[str, Any]:
    for message in reversed(messages):
        payload = _message_payload(message)
        if payload and payload.get("stage") == stage:
            result = payload.get("result")
            if isinstance(result, dict):
                return result
    return {}


def _source_text(messages: list[BaseMessage]) -> str:
    return "\n\n".join(
        str(message.content)
        for message in messages
        if isinstance(message, HumanMessage)
    )


def _stage_three_requirements(
    messages: list[BaseMessage],
) -> list[str]:
    stage_two = _stage_result(messages, 2)
    requirements = stage_two.get("requirements", [])
    discarded = set(stage_two.get("discarded_requirements", []))
    if not isinstance(requirements, list):
        return []
    return [
        requirement
        for requirement in requirements
        if isinstance(requirement, str) and requirement not in discarded
    ]


def _stage_two_defers_only_exact_version(item: Any) -> bool:
    if getattr(item, "status", None) != "unresolved":
        return False
    text = " ".join(
        str(getattr(item, field, ""))
        for field in (
            "technology",
            "application_type",
            "ui_surface",
            "runtime_platform",
            "host_model",
        )
    ).lower()
    version_markers = (
        "精确版本",
        "版本号",
        "版本兼容",
        "exact version",
        "version number",
        "version compatibility",
    )
    deferred_markers = (
        "未提供",
        "未指定",
        "待锁定",
        "实现时锁定",
        "not provided",
        "unspecified",
        "to be selected",
        "to be locked",
    )
    return any(marker in text for marker in version_markers) and any(
        marker in text for marker in deferred_markers
    )


def _validate_stage_result(
    stage: int,
    result: dict[str, Any],
    messages: list[BaseMessage] | None = None,
) -> str | None:
    if not result:
        return "结果为空"
    if stage == 2:
        try:
            parsed = StageTwoResult.model_validate(result)
        except Exception as exc:
            return f"阶段2结果不符合 StageTwoResult: {exc}"
        if set(parsed.requirements) & set(parsed.discarded_requirements):
            return "阶段2已放弃要求必须移出 requirements"
        for index, item in enumerate(parsed.compatibility_checks):
            if _stage_two_defers_only_exact_version(item):
                return (
                    f"阶段2字段 result.compatibility_checks[{index}] 把缺少精确版本标记为 "
                    "unresolved；阶段2只核验应用类型、UI载体、运行平台和宿主模型。"
                    "请删除该版本汇总项；精确版本识别、latest-stable兜底和人工确认属于阶段5。"
                )
        if any(
            item.status in {"conflict", "unresolved"}
            for item in parsed.compatibility_checks
        ) and not parsed.conflicts:
            return "阶段2存在 conflict 或 unresolved 时 conflicts 不得为空"
    if stage == 4:
        try:
            parsed = StageFourResult.model_validate(result)
        except Exception as exc:
            return f"阶段4结果不符合 StageFourResult: {exc}"
        for item in parsed.files:
            if item.status == "matched" and not item.uploaded_filename:
                return "阶段4 matched 文件必须包含 uploaded_filename"
            if item.status == "proposed" and not item.candidates:
                return "阶段4 proposed 文件必须包含 candidates"
        for item in parsed.urls:
            if item.status == "provided" and (
                not item.url or not item.url.startswith(("http://", "https://"))
            ):
                return "阶段4 provided URL 必须包含完整 http(s) URL"
            if item.status == "proposed" and not (
                (item.url and item.url.startswith(("http://", "https://")))
                or (
                    item.candidates
                    and all(
                        url.startswith(("http://", "https://"))
                        for url in item.candidates
                    )
                )
            ):
                return "阶段4 proposed URL 必须包含完整候选 URL"
    if stage == 3:
        assignments = result.get("priority_assignments")
        if assignments is not None:
            if not isinstance(assignments, list):
                return (
                    "阶段3字段 result.priority_assignments 实际值不是列表；"
                    "必须为每个稳定 requirement_id 明确返回 priority。"
                )
            expected_ids = [
                f"R{index}"
                for index in range(
                    1,
                    len(_stage_three_requirements(messages or [])) + 1,
                )
            ]
            seen: set[str] = set()
            for index, item in enumerate(assignments):
                if not isinstance(item, dict):
                    return (
                        f"阶段3字段 result.priority_assignments[{index}] 实际值为 {item!r}；"
                        "必须是包含 requirement_id 和 priority 的对象。"
                    )
                requirement_id = item.get("requirement_id")
                if requirement_id not in expected_ids:
                    return (
                        f"阶段3字段 result.priority_assignments[{index}].requirement_id "
                        f"实际值为 {requirement_id!r}；属于未知 requirement_id。"
                    )
                if requirement_id in seen:
                    return (
                        f"阶段3字段 result.priority_assignments[{index}].requirement_id "
                        f"实际值为 {requirement_id!r}；同一 requirement_id 不得重复。"
                    )
                seen.add(requirement_id)
                priority = item.get("priority")
                if type(priority) is not int or priority not in {1, 2, 3}:
                    return (
                        f"阶段3字段 result.priority_assignments[{index}].priority "
                        f"实际值为 {priority!r}；必须明确填写整数 1、2、3 之一。"
                    )
            missing = [item for item in expected_ids if item not in seen]
            if missing:
                return (
                    "阶段3字段 result.priority_assignments 缺少稳定 requirement_id："
                    f"{', '.join(missing)}。"
                )
            return (
                "阶段3 priority_assignments 已通过校验但尚未关联为最终 requirements；"
                "必须先使用阶段2不可变原文完成确定性关联。"
            )
        requirements = result.get("requirements")
        if not isinstance(requirements, list):
            return (
                "阶段3字段 result.requirements 实际值不是列表；"
                "必须返回逐项沿用阶段2保留要求的列表。"
            )
        for index, item in enumerate(requirements):
            if not isinstance(item, dict):
                return (
                    f"阶段3字段 result.requirements[{index}] 实际值为 {item!r}；"
                    "必须是包含 requirement 和 priority 的对象。"
                )
            requirement = item.get("requirement")
            if not isinstance(requirement, str) or not requirement:
                return (
                    f"阶段3字段 result.requirements[{index}].requirement "
                    f"实际值为 {requirement!r}；必须逐字填写阶段2对应的保留要求。"
                )
            priority = item.get("priority")
            if type(priority) is not int or priority not in {1, 2, 3}:
                return (
                    f"阶段3字段 result.requirements[{index}].priority "
                    f"实际值为 {priority!r}；必须明确填写整数 1、2、3 之一，"
                    "系统不会推断或默认补全。"
                )
        expected = _stage_three_requirements(messages or [])
        actual = [item["requirement"] for item in requirements]
        if actual != expected:
            if len(actual) != len(expected):
                return (
                    f"阶段3字段 result.requirements 的条目数实际为 {len(actual)}，"
                    f"阶段2保留要求为 {len(expected)} 条；必须逐项逐字沿用，不得增删。"
                )
            mismatch = next(
                index
                for index, (actual_item, expected_item) in enumerate(
                    zip(actual, expected, strict=True)
                )
                if actual_item != expected_item
            )
            return (
                f"阶段3字段 result.requirements[{mismatch}].requirement "
                f"实际值为 {actual[mismatch]!r}，期望逐字等于 {expected[mismatch]!r}；"
                "不得重新解释或静默替换。"
            )
    if stage == 5:
        try:
            parsed = StageFiveResult.model_validate(result)
        except Exception as exc:
            return f"阶段5结果不符合 StageFiveResult: {exc}"
        for index, technology in enumerate(parsed.technologies):
            try:
                _safe_segment(technology.version, "version")
            except ValueError:
                return (
                    f"阶段5字段 result.technologies[{index}].version 实际值为 "
                    f"{technology.version!r}；版本将用于知识文件名，只允许字母、数字、点、"
                    "下划线和连字符。说明文字必须放入 version_evidence，不得附加到 version。"
                )
    if stage == 6:
        knowledge = result.get("knowledge")
        if not isinstance(knowledge, list) or not knowledge:
            return "阶段6必须返回非空 knowledge 列表"
        stage_five = _stage_result(messages or [], 5)
        approved_versions: dict[str, set[str]] = {}
        for technology in stage_five.get("technologies", []):
            if not isinstance(technology, dict):
                continue
            name = technology.get("name")
            version = technology.get("version")
            if isinstance(name, str) and isinstance(version, str):
                approved_versions.setdefault(name, set()).add(version)
        for index, item in enumerate(knowledge):
            if not isinstance(item, dict):
                return f"阶段6字段 result.knowledge[{index}] 必须是对象"
            technology = item.get("technology")
            version = item.get("version")
            if not isinstance(technology, str) or not technology.strip():
                return f"阶段6字段 result.knowledge[{index}].technology 必须是非空字符串"
            if not isinstance(version, str) or not version:
                return f"阶段6字段 result.knowledge[{index}].version 必须是非空字符串"
            expected_versions = approved_versions.get(technology)
            if approved_versions and not expected_versions:
                return (
                    f"阶段6字段 result.knowledge[{index}].technology 实际值为 "
                    f"{technology!r}；该技术未出现在阶段5已批准清单中。"
                )
            if expected_versions and version not in expected_versions:
                expected = " 或 ".join(repr(value) for value in sorted(expected_versions))
                return (
                    f"阶段6字段 result.knowledge[{index}].version 实际值为 {version!r}，"
                    f"必须逐字等于阶段5为 {technology!r} 批准的版本 {expected}；"
                    "不得在 version 中附加说明文字。"
                )
            try:
                _safe_segment(version, "version")
            except ValueError:
                return (
                    f"阶段6字段 result.knowledge[{index}].version 实际值为 {version!r}；"
                    "版本将用于知识文件名，只允许字母、数字、点、下划线和连字符。"
                )
            source_url = item.get("source_url")
            content = item.get("content")
            if not isinstance(source_url, str) or not source_url.startswith(
                ("http://", "https://")
            ):
                return f"阶段6字段 result.knowledge[{index}].source_url 必须是完整 http(s) URL"
            if not isinstance(content, str) or not content.strip():
                return f"阶段6字段 result.knowledge[{index}].content 必须是非空字符串"
    if stage == 7 and (
        not isinstance(result.get("contract_markdown"), str)
        or not result["contract_markdown"].strip()
    ):
        return "阶段7必须返回 contract_markdown"
    return None


def _contains_unresolved_versions(result: Any) -> bool:
    if not isinstance(result, dict):
        return True
    technologies = result.get("technologies")
    if not isinstance(technologies, list) or not technologies:
        return True
    return any(
        not isinstance(item, dict)
        or not item.get("version")
        or str(item.get("version")).lower() == "unresolved"
        for item in technologies
    )


def _has_open_conflicts(result: Any) -> bool:
    if not isinstance(result, dict):
        return True
    checks = result.get("compatibility_checks", [])
    conflicts = result.get("conflicts", [])
    return any(
        isinstance(item, dict)
        and item.get("status") in {"conflict", "unresolved"}
        for item in checks
    ) or any(
        not isinstance(item, dict) or item.get("status") != "resolved"
        for item in conflicts
    )


def _stage_four_needs_review(result: Any) -> bool:
    if not isinstance(result, dict):
        return True
    return any(
        isinstance(item, dict) and item.get("status") in {"proposed", "unresolved"}
        for key in ("files", "urls")
        for item in result.get(key, [])
    )


def _stage_four_has_unresolved(result: Any) -> bool:
    if not isinstance(result, dict):
        return True
    return any(
        not isinstance(item, dict) or item.get("status") == "unresolved"
        for key in ("files", "urls")
        for item in result.get(key, [])
    )


def _filter_stage_four_result(
    output: WorkerOutput,
    source_text: str,
) -> WorkerOutput:
    intent = re.compile(
        r"参考|参照|依据|文档|网址|网站|链接|官网|"
        r"reference|refer(?:\s+to)?|according\s+to|"
        r"docs?|documentation|website|url|link",
        re.IGNORECASE,
    )

    def explicitly_referenced(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        url = str(item.get("url") or "")
        if url and url in source_text:
            return True
        mention = str(item.get("mention") or "").strip()
        if not mention:
            return False
        for match in re.finditer(re.escape(mention), source_text, re.IGNORECASE):
            nearby = source_text[
                max(0, match.start() - 24) : min(
                    len(source_text),
                    match.end() + 24,
                )
            ]
            if intent.search(nearby):
                return True
        return False

    result = dict(output.result)
    result["urls"] = [
        item
        for item in result.get("urls", [])
        if explicitly_referenced(item)
    ]
    return output.model_copy(update={"result": result})


def _must_revise(stage: int, draft: Any) -> bool:
    return (
        isinstance(draft, dict) and draft.get("status") == "reviewed_failed"
    ) or (stage == 2 and _has_open_conflicts(draft)) or (
        stage == 4 and _stage_four_has_unresolved(draft)
    )
