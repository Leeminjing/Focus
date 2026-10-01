"""本文件对外提供 frozen_request_messages 的无工具角色请求装配。

输入为角色基础行为、冻结正文、作用域和既有领域引用；输出为独立 instructions 消息及可信来源的参考消息。
具体工作流为保留冻结输入原文、生成稳定内容身份与引用，Round 输入不参与 WorldState diff，也不进入任务语义索引。
示例：messages = frozen_request_messages(policy, document, scope="round", source_refs=({"round_id": "r1"},))。
"""

from langchain_core.messages import HumanMessage, SystemMessage

from focus.history import content_hash


def frozen_request_messages(instructions, content, *, scope="run", source_refs=()):
    if scope not in {"run", "round"}:
        raise ValueError("冻结请求必须声明 run 或 round 作用域")
    kind = "round_decision_context" if scope == "round" else "selected_context"
    refs = [*source_refs, {"frozen_input_hash": content_hash(content)}]
    return [SystemMessage(content=instructions), HumanMessage(
        content=content, id="frozen:" + content_hash([scope, content, refs]),
        additional_kwargs={"focus_context": {"kind": kind, "origin": "runtime" if scope == "round" else "delegated",
                                              "scope": scope, "source_refs": refs}},
    )]
