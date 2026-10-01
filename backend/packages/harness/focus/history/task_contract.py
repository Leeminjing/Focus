"""本文件对外提供 task_contract_body 与 task_contract_state_update 的只读合同镜像合同。

输入为 canonical messages、可选 execution_items、兼容 task_contract 及宿主 task_contract_source；输出为最新合同正文或需同步提交的状态更新。
具体工作流为识别 typed 合同、拒绝正文冲突、从保留历史生成镜像；typed 来源失去合同时清空镜像，旧 string-only 状态明确标为 legacy。
历史手术使用 rebuilt_messages 指定新的权威消息集，跳过旧镜像正文比较并按保留合同重新生成；更新与消息一起进入 checkpoint。
本模块不批准合同、不读取子流程或数据库、不从 XML 正文推断类型。示例：update = task_contract_state_update(state, rebuilt_messages=retained)。
"""

from collections.abc import Iterable, Mapping

from langchain_core.messages import BaseMessage


def task_contract_body(messages: Iterable[BaseMessage]) -> str | None:
    contracts = [message for message in messages
                 if message.additional_kwargs.get("focus_context", {}).get("kind") == "task_contract"]
    return contracts[-1].content if contracts else None


def task_contract_state_update(state: Mapping, *, rebuilt_messages: Iterable[BaseMessage] | None = None) -> dict:
    original = task_contract_body(state.get("messages", ()))
    rebuilding = rebuilt_messages is not None
    body = task_contract_body(rebuilt_messages) if rebuilding else original
    source = state.get("task_contract_source")
    if source not in {None, "typed", "legacy"}:
        raise ValueError("task_contract_source 缺少有效宿主来源")
    typed = body is not None or original is not None or source == "typed" or any(
        item.get("kind") == "task_contract" for item in state.get("execution_items") or ()
    )
    existing = state.get("task_contract")
    if typed:
        if not rebuilding and body is not None and existing is not None and existing != body:
            raise ValueError("task_contract 兼容视图与 canonical 合同不一致")
        desired = {"task_contract": body, "task_contract_source": "typed"}
    elif existing:
        desired = {"task_contract": existing, "task_contract_source": "legacy"}
    else:
        return {}
    return {key: value for key, value in desired.items() if key not in state or state[key] != value}
