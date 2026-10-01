"""本文件对外提供 execution Items 同步、幂等状态 reducer 和显式新分支投影。

输入为已提交 typed 历史、LangGraph messages 或宿主 ExecutionHistoryRebuild；输出为唯一可验证的 execution Items 更新。
具体工作流为拒绝对既有 Item 的无声明改写、收录新增消息，并在显式历史手术中清除 opaque 与运行控制。
显式重建在 reducer 核对源历史 hash 后原子替换 Items，与 messages/snapshot 随同一 checkpoint 提交。
示例：synchronize_items(state.get("execution_items"), state["messages"])；新分支使用 branch_messages(messages)。
"""

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable

from langchain_core.messages import AIMessage, BaseMessage

from focus.history.codec import history_records, messages_to_items
from focus.history.contracts import FocusItem, content_hash
from focus.history.protocol import validate_items


@dataclass(frozen=True)
class ExecutionHistoryRebuild:
    source_hash: str
    items: list[dict]


def replace_execution_items(existing: list[dict] | None, new: list[dict] | None | ExecutionHistoryRebuild) -> list[dict] | None:
    if new is None:
        return None
    rebuilding = isinstance(new, ExecutionHistoryRebuild)
    if rebuilding:
        if new.source_hash != content_hash(existing):
            raise ValueError("execution 分支重建的源历史已变化")
        new = new.items
    parsed = tuple(FocusItem.model_validate(raw) for raw in new)
    if len({item.item_id for item in parsed}) != len(parsed):
        raise ValueError("execution authority Item identity 重复")
    previous = {} if rebuilding else {raw["item_id"]: raw for raw in existing or ()}
    for raw in new:
        if raw["item_id"] in previous and previous[raw["item_id"]] != raw:
            raise ValueError("已提交 execution Item 不可原位修改；历史手术必须显式重建分支")
    return deepcopy(new)


def synchronize_items(previous: list[dict] | None, messages: Iterable[BaseMessage]) -> list[dict[str, Any]]:
    items = messages_to_items(messages)
    records = [item.model_dump(mode="json") for item in items]
    if previous is not None and records[:len(previous)] != previous:
        raise ValueError("messages bridge 与 typed authority 不一致；缺少显式分支重建")
    validate_items(items, require_closed=False)
    return records


def branch_messages(messages: Iterable[BaseMessage]) -> list[BaseMessage]:
    result = []
    for original in messages:
        message = original.model_copy(deep=True)
        context = message.additional_kwargs.get("focus_context", {})
        if context.get("scope") in {"runtime", "round", "run"} or context.get("origin") == "runtime":
            continue
        if isinstance(message, AIMessage):
            message.additional_kwargs.pop("focus_response_items", None)
            message.additional_kwargs.pop("reasoning_content", None)
            message.response_metadata = {}
            if isinstance(message.content, list):
                message.content = [part for part in message.content if not isinstance(part, dict)
                                   or part.get("type") not in {"reasoning", "compaction"}]
        result.append(message)
    return result


def checkpoint_records(values: dict) -> tuple[dict, ...]:
    records = synchronize_items(values.get("execution_items"), values.get("messages", ()))
    return history_records(FocusItem.model_validate(raw) for raw in records)
