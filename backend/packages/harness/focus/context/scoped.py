"""本文件对外提供 FrozenContext、scoped_messages 与 prepare_scoped_contexts。

输入为 admission 时冻结的正文、来源版本和受治理 Run 身份；输出为有稳定身份的引用或执行 policy Items。
具体工作流为验证内容哈希，在最终 checkpoint 准备时按稳定身份补齐当前 Run 的选中上下文，
并拒绝同一冻结身份下的正文改写；sampling 时仅投影当前 Run 的临时块。
历史仍完整保留用于审计；选中记忆/技能是 reference_only，材料读取规则不产生任务事实。
示例：updates = prepare_scoped_contexts(messages, (FrozenContext("memory", "已选记忆"),), "run-1")。
"""

from dataclasses import dataclass
from copy import deepcopy
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage

from focus.history import content_hash


@dataclass(frozen=True)
class FrozenContext:
    name: str
    content: str
    source_refs: tuple[dict, ...] = ()
    authority: Literal["reference", "policy"] = "reference"

    @classmethod
    def from_record(cls, raw: dict):
        if raw.get("content_hash") != content_hash(raw["content"]):
            raise ValueError("冻结上下文内容哈希不匹配")
        return cls(raw["name"], raw["content"], tuple(raw.get("source_refs", ())), raw.get("authority", "reference"))

    def record(self) -> dict:
        return {"name": self.name, "content": self.content, "source_refs": list(self.source_refs),
                "authority": self.authority, "content_hash": content_hash(self.content)}


def scoped_messages(messages, run_id: str):
    return [message for message in messages
            if message.additional_kwargs.get("focus_context", {}).get("scope") != "run"
            or any(ref.get("run_id") == run_id for ref in
                   message.additional_kwargs.get("focus_context", {}).get("source_refs", ()))]


def prepare_scoped_contexts(messages, contexts: tuple[FrozenContext, ...], run_id: str):
    if contexts and not run_id:
        raise ValueError("冻结上下文需要精确 Run 身份")
    retained = {message.id: message for message in messages}
    updates = []
    for context in contexts:
        if not context.content:
            continue
        digest = content_hash(context.record())
        identity = f"context:{run_id}:{digest}"
        policy = context.authority == "policy"
        message_type = SystemMessage if policy else HumanMessage
        message = message_type(content=context.content, id=identity, additional_kwargs={"focus_context": {
            "kind": "message" if policy else "selected_context", "origin": "runtime" if policy else "delegated",
            "authority": "policy" if policy else "reference", "scope": "run",
            "source_refs": [*context.source_refs, {"run_id": run_id, "content_hash": digest}],
        }})
        if identity in retained:
            existing = retained[identity]
            existing_kwargs = deepcopy(existing.additional_kwargs)
            existing_kwargs.get("focus_context", {}).setdefault("authority", "policy" if policy else "reference")
            if existing.content != message.content or existing_kwargs != message.additional_kwargs:
                raise ValueError("已冻结上下文与 retained checkpoint 不一致")
            continue
        updates.append(message)
        retained[identity] = message
    return updates
