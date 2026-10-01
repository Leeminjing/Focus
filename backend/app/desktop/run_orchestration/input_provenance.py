"""本文件对外提供 bind_run_inputs，生产宿主确认的用户／委托输入来源。

输入为服务端构造的 DesktopRun 和本次新输入消息；输出为有稳定消息身份、来源及 Run/Revision/指令引用的副本。
具体工作流为只绑定本次输入，不改写历史 checkpoint；Main/Teammate 的 human 来源取自 Run 登记身份，
调用方提交的 focus_context 不能提升信任。Patrol 的冻结认知输入维持其独立合同。
示例：run.input_messages = bind_run_inputs(run)；执行装配使用 bind_run_inputs(run, messages)。
"""

from copy import deepcopy


def bind_run_inputs(run, messages=None):
    records = deepcopy((run.input_messages if messages is None else messages) or [])
    if run.kind not in {"main", "teammate", "worker"}:
        return records
    origin = "direct_user" if run.origin == "direct_user" else "delegated"
    reference = _source_reference(run)
    for ordinal, record in enumerate(records):
        if not isinstance(record, dict) or record.get("role") not in {"human", "user"}:
            continue
        origin_message_id = getattr(run, "origin_message_id", None)
        if origin_message_id and record.get("id") not in {None, origin_message_id}:
            continue
        identity = record.get("id") or origin_message_id or f"input:{run.run_id}:{ordinal}"
        record["id"] = identity
        kwargs = deepcopy(record.get("additional_kwargs", {}))
        kwargs["focus_context"] = {"origin": origin, "scope": "execution", "kind": "message",
                                  "authority": "task", "source_refs": [{**reference, "message_id": identity}]}
        record["additional_kwargs"] = kwargs
        if "_lc" in record:
            record["_lc"]["data"]["id"] = identity
            record["_lc"]["data"]["additional_kwargs"] = deepcopy(kwargs)
    return records


def _source_reference(run):
    reference = {name: getattr(run, name, None) for name in (
        "run_id", "task_id", "execution_thread_id", "checkpoint_ns", "context_revision_id",
        "context_checkpoint_id", "directive_id", "loop_id", "round_id", "action_id", "user_intent_id",
    )}
    equipment = getattr(run, "equipment", None) or {}
    reference["context_checkpoint_id"] = reference["context_checkpoint_id"] or equipment.get("_durable_dispatch_execution", {}).get("checkpoint_id")
    reference["input_origin"] = run.origin
    return {key: value for key, value in reference.items() if value is not None}
