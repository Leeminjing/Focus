# Verification Report: migrate-focus-context-to-responses

验证日期：2026-10-01。用户明确选择此 change 后，依据 openspec-verify-change 检查任务、六份 delta specs、design 和实际实现。本轮未修改应用代码，未修改任务勾选，未执行归档。

**结论：发现 1 个 CRITICAL 和 5 个 WARNING；当前不满足归档条件。** 56/56 的任务勾选和已有场景映射不能代替组合执行路径的验收。

## Summary

| Dimension | Status |
|---|---|
| Completeness | tasks 56/56 已勾选；37/37 Requirements 有实现入口；69/69 Scenarios 有可解析的测试引用 |
| Correctness | 本轮 backend 161 通过、desktop 180 通过；额外 6 个独立复现全部确认合同偏差，涉及 9 个 Requirements |
| Coherence | history、context、models、Desktop publication 的模块划分已形成；来源权威、连续前缀及压缩后的当前 Run 重建存在偏差 |

“有实现入口”表示已定位相关实现，不能理解为全部条款通过。其余 28 个 Requirements 在本轮检查范围内未发现具体偏差，也不代表已执行所有生产环境场景。

## CRITICAL — Must fix before archive

### F1 [P1] 压缩恢复会丢掉当前 Run 的选定上下文

**触发：** 当前 Run 已冻结 Memory、selected Skill 和材料读取 policy，模型调用前触发 CompressionGate。用户只压缩一条无关的旧任务消息，然后恢复图执行。

**实际结果：** 三块当前 Run 上下文在压缩 interrupt 前都在 checkpoint 中；恢复后的首次 sampling 全部缺失，最终 checkpoint 也没有它们。模型可以在这个缺失约束的请求上直接给出最终答案。

**原因：** [lead/agent.py:185](C:/Users/brubing/Desktop/ag-project/focus/backend/packages/harness/focus/agents/lead/agent.py:185) 将 ScopedContextMiddleware 排在 CompressionGate 前；[gate.py:226](C:/Users/brubing/Desktop/ag-project/focus/backend/packages/harness/focus/agents/compression/gate.py:226) 重建整个 messages 时使用 [branch_messages:45](C:/Users/brubing/Desktop/ag-project/focus/backend/packages/harness/focus/history/bridge.py:45)，它过滤所有 run/runtime/round 范围消息。LangGraph 恢复重新执行被 interrupt 的节点，不重新执行已经成功的 ScopedContext 节点；后续 wrap 只过滤消息，不重新注入冻结内容。

**证据：** `verify-reproductions.json → compression_resume`，使用真实 create_agent、InMemorySaver、同步 durability、现有中间件和 fake model：

```json
{
  "interrupted": true,
  "model_calls": 1,
  "selected_contexts_before_resume": [
    "CURRENT_RUN_MEMORY", "CURRENT_RUN_SKILL", "CURRENT_RUN_MATERIAL_POLICY"
  ],
  "selected_contexts_in_first_sampling": [],
  "selected_contexts_in_final_checkpoint": []
}
```

**要求／设计：** scoped-agent-context 的 Context assembly、Selected context bindings；revision-semantic-selection 的 Selected references；design §6、§11。任务 3.6、5.6、6.2、9.2、9.5 的验收需要补组合路径证据。

**修复建议：** 将显式新分支的状态清理与当前 Run 的请求重建分开，在所有历史重建之后、准备 checkpoint 之前统一重新组装当前冻结 bindings。保持已有上下文身份去重及 typed authority 同步，避免在 wrap 层临时补正文绕过耐久 checkpoint。增加真实 graph 的 compression interrupt→apply→首个模型调用回归，并验证 Memory、Skill、policy 各一次且仍指向原冻结版本。

## WARNING — Should fix

### F2 [P1] Curator 可以把 reference_only 引用投影到平台指令层级

**触发与结果：** 将 reference_only Memory 来源交给 Curator，合法 ComposeMessage 选择 `role=system`。编译器保留 `kind=selected_context`、`origin=curator`，但 Responses 投影发送 OpenAI `developer`／DeepSeek `system`。

**位置：** [contract.py:268](C:/Users/brubing/Desktop/ag-project/focus/backend/app/desktop/context_curator/contract.py:268) 识别引用资格；[response_projection.py:87](C:/Users/brubing/Desktop/ag-project/focus/backend/packages/harness/focus/models/response_projection.py:87) 仅将 delegated/collaborator/tool 降到 user，忽略 selected_context 及其引用权威。

**证据：** `reference_policy_projection.projected_roles = {openai: developer, deepseek: system}`。这证明模型指令层级提升；本轮没有证明真实工具授权绕过，宿主 SecurityContext 裁决仍存在。

**要求：** Policy authority survives provider role differences；Typed items preserve identity and trusted provenance；Selected context bindings are immutable and attributable；design §1、§6、§8。

**修复建议：** 由宿主确认的 authority／Item kind 决定 policy 投影，限制 Curator 的 role 选择不能提升 reference 来源；覆盖 copy/compose、两种 Provider 及 mixed-source 派生。不要仅增加正文过滤规则。当前引用继承测试选择 human role，未覆盖这个 system 分支。

### F3 [P1] 新输入未把 direct_user 与 delegated_patrol 来源写入 FocusItem

**实际结果：** 对两种已知运行来源，Main 输入的消息字典均只有 role/content/id；经执行器与 typed bridge 后，两者均变成 `origin=legacy_unknown`、空 source_refs。新 V2 历史无法凭 Item 恢复真实用户和 Patrol 委托的区别。

**位置：** [dispatch.py:282](C:/Users/brubing/Desktop/ag-project/focus/backend/app/desktop/agent_loop/dispatch.py:282) 创建 delegated_patrol Run；[service.py:1198](C:/Users/brubing/Desktop/ag-project/focus/backend/app/desktop/service.py:1198) 创建当前消息；[run_material_message.py:30](C:/Users/brubing/Desktop/ag-project/focus/backend/app/desktop/run_material_message.py:30) 未携带来源；[executor.py:130](C:/Users/brubing/Desktop/ag-project/focus/backend/app/desktop/run_orchestration/executor.py:130) 仅反序列化输入；[codec.py:69](C:/Users/brubing/Desktop/ag-project/focus/backend/packages/harness/focus/history/codec.py:69) 默认 legacy_unknown。

**证据：** `input_provenance` 中 direct_user 和 delegated_patrol 均为 legacy_unknown。DesktopRun 本身仍保留 origin，Kernel/grant 路径仍存在；问题是新 typed history 没有满足来源合同，不能把它表述成当前已经放宽执行权限。

**要求／缺失测试：** Typed items preserve identity and trusted provenance 的 “A delegated instruction looks like human input”。现有映射指向的测试只比较真实样式 HumanMessage 与 runtime fragment，没有走服务端 Patrol admission→Main history。

**修复建议：** 在受信任的宿主 admission／dispatch 边界生产 Item provenance，附上 Run、directive、Round 和来源 checkpoint 等可用引用；使直接用户输入与委托输入进入同一无损 codec 时仍可区分。调用方正文和 wire role 不得自行指定受信任来源。

### F4 [P1] 材料投影改写 replay 前缀后，仍无条件回传旧 opaque continuation

**实际结果：** 历史 HumanMessage 中的图像被本轮 image binding 投影为文字占位；它后面的保存 reasoning `encrypted_content` 仍被 replay。适配器只检查 requested_model、provider、projection_version，没有验证有效输入前缀／材料绑定是否变化。

**位置：** [image_attachment.py:81](C:/Users/brubing/Desktop/ag-project/focus/backend/packages/harness/focus/agents/image_attachment.py:81) 改写旧图片；[response_projection.py:66](C:/Users/brubing/Desktop/ag-project/focus/backend/packages/harness/focus/models/response_projection.py:66) 接受原生 continuation。类似问题也适用于过滤上一 Run 的 selected context。

**证据：** `changed_image_prefix` 中 `image_prefix_changed=true`、`native_reasoning_replayed=true`、`projection_rejected=false`。这是已确认的 Focus 合同缺失；本轮未调用 OpenAI，不能宣称该 Provider 必然拒绝或必然误推理。

**要求／设计：** Continuation is scoped to an eligible execution branch；design §6 明确要求改变材料前缀后检查 continuation，§11 限于合法连续前缀。

**修复建议：** 将实际 Provider 输入前缀及本轮材料／selected 投影合同纳入 continuation 资格证明。变化时在权威执行层显式重建或拒绝，而非仅在请求中删除 reasoning。补 unchanged-prefix 成功、image-prefix 改写和 Run-scope 过滤的边界测试。

### F5 [P2] V1 Reader 经 UI codec 适配，丢失可证明的 runtime 来源

**触发：** V1 checkpoint 的 BaseMessage 已带宿主 focus_context 元数据，其中一条是 runtime/world_state_update。

**实际结果：** Reader 请求 semantic 时，先读取 execution；[reader.py:321](C:/Users/brubing/Desktop/ag-project/focus/backend/app/desktop/context_evolution/reader.py:321) 用 UI serialize_message 导出运行消息，再调用 legacy adapter。宿主元数据已被裁掉，环境控制消息被当作 legacy普通消息，以 `semantic_policy=index` 返回。

**证据：** `v1_semantic_runtime` 中真实任务与 `environment-control` 都进入 semantic view。直接对原 BaseMessage 做 typed 转换可正确得到 origin=runtime；因此这是 reader 组合路径的损失。该复现用带明确元数据的 V1 fixture，不推断所有旧数据都有可证明来源。

**要求：** Legacy revisions are adapted without rewriting authority；Selection eligibility applies across the entire indexing pipeline；design §2、§3、§4。

**修复建议：** V1 semantic 读取从原 checkpoint 对象／canonical history codec 适配，再进行 semantic selection；UI codec 只用于 display。保留无可信元数据的旧消息为 legacy_unknown，避免从正文猜测。补 V1 checkpoint 已知 runtime/source 元数据与未知普通文本的混合用例，并断言旧数据/hash 不变。

### F6 [P2] 压缩工具协议 repair 仍使用 success 状态

**触发与结果：** 压缩删除真实工具输出，同时保留调用。[_repair_protocol:118](C:/Users/brubing/Desktop/ag-project/focus/backend/packages/harness/focus/agents/compression/gate.py:118) 插入合成 ToolMessage，没有显式 error status，因此 LangChain 默认 `status=success`。它虽被识别为 runtime/projection_repair 并排除于 semantic，却仍在保存的工具结果状态上声称 success。

**证据：** `compression_repair_status = {synthetic: true, tool_status: success, item_origin: runtime, item_kind: projection_repair}`。

**要求／设计：** Authored and execution histories have separate authority；design §2 的 repair 必须有 error 语义；任务 2.4、10.3 要求统一底层协议职责。现有 interruption repair 测试覆盖另一条 typed repair 路径，没有覆盖 compression repair。

**修复建议：** 让压缩和其他修复路径共享可审计的 typed repair 合同，明确 error／omitted 原因及来源，不将占位当执行成功。保留 curation_synthetic/exclude 资格，补压缩拆开并行工具交换的回归。

## Scenario coverage assessment

六份 specs 共 69 个场景；已有 scenario-coverage.json 的每一项都能找到具名测试，但该检查仅证明引用有效。报告未把 69 项全部标记为行为通过。

本轮确定缺少以下组合条件的持久回归测试，分别归入 F1–F6：

1. 当前 Run selected bindings 已落 checkpoint 后，真实 compression interrupt/apply 的首个 sampling。
2. reference_only 来源经 Curator system ComposeMessage，再经双 Provider role 投影。
3. 受治理 direct_user／delegated_patrol admission 到 typed history 的来源闭环。
4. image／Run-scope 请求前缀变化与保存 continuation 的资格校验。
5. V1 Reader 的 canonical 来源适配到 semantic pipeline。
6. compression repair 的工具 error 状态，而非只检查 interruption repair。

临时复现脚本提供现状证据，尚未作为修复后的自动回归测试提交。场景修复后应更新精确测试映射，保留本次反例证据。

## Design coherence

已定位到独立 FocusItem/codec/selection/protocol、WorldState full/diff 与 checkpoint bridge、Responses 请求/输出/stream 模块、Desktop publication 与 inbox/attempt 端口。未发现 Harness history 反向依赖 Desktop ORM，也没有通过 previous_response_id/conversation 引入第二套 Provider 上下文权威。

主要偏差集中在：design §1 的宿主来源未在新输入生产点落地（F3），§2 的 canonical legacy 路径与 repair error（F5、F6），§6 的当前 Run 上下文恢复与引用权威（F1、F2），§11 的合法连续前缀证明（F4）。修复应收敛这些职责的共同边界，避免在各调用点重复追加 prompt 补丁。

## Validation evidence and limitations

本轮实际执行：

- Backend：15 个专项测试文件，**161 passed in 21.36s**。覆盖 history、typed checkpoint、WorldState、scoped/inbox、Responses、attempt audit、迁移、Reader、Curator、must-view、sandbox、架构、evidence resolver 和 Patrol。
- Desktop：全部 `*.test.cjs` 加 loop-live-projection/map-collapsible-view 两个 JS 文件，**180 passed，0 failed**。
- `openspec validate migrate-focus-context-to-responses --strict`：通过。
- `git diff --check`：通过；只有现有 Windows 换行提示。
- 额外只读复现：6 项均执行成功并返回上文偏差，不需要 Provider 密钥，不修改数据库或用户工作区文件。

日志：[backend](C:/Users/brubing/Desktop/ag-project/focus/.tmp/openspec-verify-responses-tests.txt)、[desktop](C:/Users/brubing/Desktop/ag-project/focus/.tmp/openspec-verify-desktop-tests.txt)。

复现命令：

```powershell
& 'C:/Develop/python/python312/python.exe' .tmp/verify_responses_boundaries.py
```

LangGraph interrupt 恢复语义已通过本轮 Context7 核对：[官方 interrupts 文档](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/interrupts.mdx)。复现使用内存 checkpointer 验证中间件节点行为，不等同于生产进程崩溃实验。

此前实现记录保留在 verification.md/test-results.json：全量 backend 曾为 1584 passed、20 failed、6 skipped；其中 13 个失败在独立 HEAD 基线复现，另外 7 个已有专项修复重测。**本轮未重新跑完整 backend 套件，不能宣布完整套件全绿。** 既有 114 文件静态检查、DeepSeek live smoke 是此前证据，本轮未重跑；OpenAI live 与交互 Electron E2E 仍未运行。

## Final assessment

**1 critical issue found. Fix before archiving.** 先修 F1 并补真实 graph 回归；F2–F6 的来源、投影与修复合同也应在声明此迁移完整前解决或明确修订已批准设计。现有任务勾选保留为实施者记录，本报告提供独立的验收差异。

结构化逐 Requirement 实现定位及 69 个场景测试引用检查见 [verify-report.json](C:/Users/brubing/Desktop/ag-project/focus/openspec/changes/migrate-focus-context-to-responses/verify-report.json)；六个实际结果见 [verify-reproductions.json](C:/Users/brubing/Desktop/ag-project/focus/openspec/changes/migrate-focus-context-to-responses/verify-reproductions.json)。
