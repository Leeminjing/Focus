"""本文件对外提供 KnowledgeSnapshot、ContractApproval 与 compile_handoff、validate_contract_mirror。

输入为宿主冻结的知识／人工批准、完成子图状态、原触发输入及精确 checkpoint 来源；输出为独立合同和参考 HumanMessages。
具体工作流为验证第七阶段批准与内容身份、核对冻结知识、生成稳定交付身份并绑定来源；不读取文件、不批准权限、不写数据库。
兼容 task_contract 只核对 canonical 合同正文；无 typed 合同的旧状态保持只读兼容，不按 XML 标签重分类。
示例：messages = compile_handoff(completed, trigger, checkpoint_ref, source_refs)。
"""

from copy import deepcopy
from typing import Any, Literal

from langchain_core.messages import BaseMessage, HumanMessage
from pydantic import BaseModel, ConfigDict, Field, model_validator

from focus.history import content_hash
from focus.history.task_contract import task_contract_body, task_contract_state_update


class KnowledgeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    content: str = Field(min_length=1)
    content_hash: str
    technology: str
    version: str
    source_url: str

    @model_validator(mode="after")
    def validate_content(self):
        if self.content_hash != content_hash(self.content):
            raise ValueError("冻结知识内容 hash 不一致")
        return self


class ContractApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: Literal[7] = 7
    status: Literal["approved"] = "approved"
    artifact_ref: str = Field(min_length=1)
    contract_hash: str
    artifact_hash: str


def _approved_contract(state: dict[str, Any]) -> tuple[str, ContractApproval]:
    if state.get("stage") != 9 or state.get("awaiting_human"):
        raise ValueError("承诺流程尚未完成或等待人工批准")
    approval = ContractApproval.model_validate(state.get("contract_approval"))
    artifact = state.get("artifacts", {}).get("7", {})
    contract = state.get("task_contract", "")
    if not isinstance(contract, str) or not contract.strip():
        raise ValueError("承诺流程缺少合同正文")
    approved = artifact.get("contract_markdown") if isinstance(artifact, dict) else None
    if (not isinstance(approved, str) or contract != approved.strip()
            or approval.contract_hash != content_hash(contract)
            or approval.artifact_hash != content_hash(artifact)):
        raise ValueError("合同与第七阶段批准 artifact 不一致")
    return contract, approval


def _message(identity: str, content: str, kind: str, refs: list[dict]) -> HumanMessage:
    return HumanMessage(id=identity, content=content, additional_kwargs={"focus_context": {
        "kind": kind, "origin": "delegated", "scope": "revision", "source_refs": deepcopy(refs),
    }})


def compile_handoff(state: dict[str, Any], trigger: HumanMessage, checkpoint_ref: dict,
                    source_refs: list[dict]) -> list[HumanMessage]:
    contract, approval = _approved_contract(state)
    if not trigger.id or not all(checkpoint_ref.get(key) for key in ("thread_id", "checkpoint_id")):
        raise ValueError("合同交付缺少触发输入或精确 child checkpoint")
    child_messages = state.get("messages", [])
    if not child_messages or child_messages[0].id != trigger.id:
        raise ValueError("承诺 child checkpoint 与触发输入不一致")
    snapshots = [KnowledgeSnapshot.model_validate(raw) for raw in state.get("knowledge_snapshots", [])]
    paths = [snapshot.path for snapshot in snapshots]
    if not snapshots or paths != state.get("knowledge_files") or len(paths) != len(set(paths)):
        raise ValueError("承诺流程缺少可验证的冻结知识来源")
    base = [*source_refs, {"kind": "commitment_input", "message_id": trigger.id},
            {"kind": "commitment_checkpoint", **checkpoint_ref}]
    proof = {"kind": "commitment_approval", **approval.model_dump(mode="json")}
    identity = "contract:" + content_hash([trigger.id, checkpoint_ref["thread_id"], proof])
    contract_message = _message(identity, contract, "task_contract", [*base, proof])
    contract_message.additional_kwargs["focus_context"]["display_replaces"] = trigger.id
    references = []
    for snapshot in snapshots:
        ref = {"kind": "commitment_knowledge", **snapshot.model_dump(exclude={"content"})}
        reference = _message("knowledge:" + content_hash([identity, ref]), snapshot.content,
                             "selected_context", [*base, ref])
        reference.additional_kwargs["focus_context"].update(display_parent=identity, display_source=snapshot.path)
        references.append(reference)
    return [contract_message, *references]


def validate_contract_mirror(messages: list[BaseMessage], contract: str | None) -> str | None:
    task_contract_state_update({"messages": messages, "task_contract": contract})
    return task_contract_body(messages)
