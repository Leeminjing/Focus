"""本文件对外提供 display_messages，生成不改变 canonical history 的消息展示视图。

输入为完整无损 BaseMessages；输出为排除内部上下文、按声明关系展示合同及配套引用的消息副本。
具体工作流为收集合同替换关系与冻结参考，将合同映射到原展示 ID，保留旧的合同／理论依据排版并隐藏参考重复行。
原触发输入的 files 附件只读复制到替换展示行；不把附件元数据写入 canonical 合同，不改变实际请求材料绑定。
原用户输入、合同 ID、来源和知识正文在权威历史中保持不变；本函数不用于模型执行或 persistence。
示例：display_messages(items_to_messages(payload.execution_items)) 交给 UI codec。
"""

from collections.abc import Iterable
from copy import deepcopy

from langchain_core.messages import BaseMessage


def display_messages(messages: Iterable[BaseMessage]) -> list[BaseMessage]:
    messages = list(messages)
    by_id = {message.id: message for message in messages if message.id}
    replacements = {message.additional_kwargs.get("focus_context", {}).get("display_replaces")
                    for message in messages
                    if message.additional_kwargs.get("focus_context", {}).get("kind") == "task_contract"}
    references: dict[str, list[BaseMessage]] = {}
    for message in messages:
        context = message.additional_kwargs.get("focus_context", {})
        if context.get("kind") == "selected_context" and context.get("display_parent"):
            references.setdefault(context["display_parent"], []).append(message)
    visible = []
    for message in messages:
        context = message.additional_kwargs.get("focus_context", {})
        if (message.id in replacements or context.get("scope") in {"runtime", "round"}
                or context.get("kind") == "selected_context"):
            continue
        if context.get("kind") == "task_contract" and context.get("display_replaces"):
            sections = [f"<task_contract>\n{message.content}\n</task_contract>"]
            for reference in references.get(message.id, []):
                source = reference.additional_kwargs["focus_context"]["display_source"]
                sections.append(f'<theoretical foundation source="{source}">\n{reference.content}\n</theoretical foundation>')
            kwargs = deepcopy(message.additional_kwargs)
            original = by_id.get(context["display_replaces"])
            if original is not None and original.additional_kwargs.get("files"):
                kwargs["files"] = deepcopy(original.additional_kwargs["files"])
            message = message.model_copy(update={"id": context["display_replaces"], "content": "\n\n".join(sections),
                                                 "additional_kwargs": kwargs})
        visible.append(message)
    return visible
