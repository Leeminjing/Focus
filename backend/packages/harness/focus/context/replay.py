"""本文件对外提供 continuation_rebuild_reason 与 rebuilt_prefix。

输入为 checkpoint 消息、实际 Run 投影和显式 Provider 合同；输出为兼容结果或可审计的新分支任务前缀。
具体工作流为按当前 selected/image 选择构造实际输入，检查每个原生组的证明；不兼容时移除旧 runtime/opaque，
保留任务正文与真实工具交换，并以共享 error repair 闭合缺失结果，记录原输出身份和重建原因。
示例：reason = continuation_rebuild_reason(messages, context, instructions, contract, model, tools)。
"""

from langchain_core.messages import HumanMessage, SystemMessage

from focus.agents.image_attachment import project_image_messages
from focus.context.scoped import scoped_messages
from focus.history.bridge import branch_messages
from focus.history.contracts import content_hash
from focus.history.repair import repair_tool_messages
from focus.models.response_continuation import ContinuationMismatch
from focus.models.response_projection import ResponsesRequestProjector


def continuation_rebuild_reason(messages, context, instructions, contract, model, tools):
    if contract is None or not any(message.additional_kwargs.get("focus_response_items") is not None for message in messages):
        return None
    projected = project_image_messages(scoped_messages(messages, str(context.get("run_id", ""))), context)
    try:
        ResponsesRequestProjector(contract).build([SystemMessage(content=instructions), *projected], model=model, tools=tools)
    except ContinuationMismatch as error:
        return str(error)
    return None


def rebuilt_prefix(messages, reason):
    sources = [message.id for message in messages if message.additional_kwargs.get("focus_response_items") is not None]
    reference = {"cause": reason, "source_response_ids": sources, "source_history_hash": content_hash([
        [message.id, message.content, message.response_metadata.get("request_source_manifest")]
        for message in messages
    ])}
    audit = HumanMessage(id="execution-rebuild:" + content_hash(reference),
        content="Focus execution branch rebuilt from retained task messages; previous Provider continuation no longer applies.",
        additional_kwargs={"focus_context": {"kind": "message", "origin": "runtime", "scope": "execution",
                                              "source_refs": [reference]}})
    return [*repair_tool_messages(branch_messages(messages), cause="provider_prefix_rebuild"), audit]
