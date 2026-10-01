"""本文件对外提供 continuation_prefix、validate_replay_prefix 与 ContinuationMismatch。

输入为实际 Responses 请求，或保存的输出组及本次已投影前缀；输出为无正文的前缀证明或明确的不兼容错误。
具体工作流为绑定 instructions、输入 Item 顺序/内容与版本；opaque reasoning/compaction 仅在证明匹配时回放。
没有证明的旧输出要求显式新分支，不能凭可见 summary 或 Provider/model 名称假定兼容。
示例：proof = continuation_prefix(payload)；validate_replay_prefix(message, projected_prefix, instructions)。
"""

from focus.history.contracts import content_hash


PREFIX_VERSION = "focus-responses-prefix-v1"


class ContinuationMismatch(ValueError):
    pass


def continuation_prefix(payload):
    return {"version": PREFIX_VERSION, "input_count": len(payload["input"]),
            "input_hash": content_hash(payload["input"]), "instructions_hash": content_hash(payload.get("instructions"))}


def validate_replay_prefix(message, prefix, instructions):
    native = message.additional_kwargs.get("focus_response_items", ())
    if not any(item.get("type") in {"reasoning", "compaction"} for item in native):
        return
    manifest = message.response_metadata.get("request_source_manifest") or {}
    proof = manifest.get("continuation_prefix")
    current = continuation_prefix({"input": prefix, "instructions": instructions})
    if proof != current:
        raise ContinuationMismatch(f"输出组 {message.id or 'unknown'} 缺少匹配的输入前缀证明；需要显式新 execution 分支")
