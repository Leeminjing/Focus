## Purpose

定义 Responses typed 事件如何收束为无损执行 Items 和 Focus 的既有可见事件，并区分 Provider 成功、不完整、失败、断流与取消，使流式展示、工具执行、用量记录和持久化恢复不会因协议切换而产生重复输出或错误成功状态。

## ADDED Requirements

### Requirement: Stream normalization preserves channel and item boundaries

系统 SHALL 按 Provider 合同区分可见 assistant text、可见 reasoning、调用参数、工具结果和控制事件。tokens 与 reasoning SHALL 只承载允许的 assistant 增量，其他输出经角色化 snapshot 或独立活动事件投递；MUST NOT 对任意含 text 字段的内容块盲目拼接正文。

#### Scenario: Interleave reasoning and visible text
- **WHEN** 流中交错出现 reasoning 与 output text 增量
- **THEN** 两者进入不同展示通道，原生 Item 顺序与身份仍能在持久化时恢复

#### Scenario: Receive tool arguments or tool output
- **WHEN** 流事件携带调用参数，或图执行产生工具结果
- **THEN** 不进入助手正文或 reasoning 通道，工具关联与既有承诺子图隔离保持有效

### Requirement: Tool calls execute only after valid argument completion

调用参数增量 SHALL 按其 output／call 身份组装。工具执行前 SHALL 获得协议完整且验证有效的参数；未完成、无效或冲突的调用 MUST NOT 触发工具副作用。终态重放 MUST NOT 再次执行已经持久化完成的调用。

#### Scenario: Disconnect during arguments
- **WHEN** function arguments 只收到部分 JSON 就断流
- **THEN** 该部分不作为完整工具调用执行，尝试进入可诊断中断或失败状态

#### Scenario: Repeat an item completion event
- **WHEN** 恢复处理再次遇到已保存的调用完成事件
- **THEN** 依据稳定身份去重，不重复提交调用或重复执行工具

### Requirement: Provider terminal status controls model attempt settlement

系统 SHALL 显式区分 completed、incomplete、failed、取消和未知终态断流。流关闭、存在 token 或输出可解析 MUST NOT 自动表示成功。incomplete 或拒绝 SHALL 进入有界处理或可诊断失败／中断，不能直接发布结构化领域结果或成功结束 Run。

#### Scenario: Receive incomplete output with valid JSON prefix
- **WHEN** Provider 终态为 incomplete，即使部分正文可解析
- **THEN** 该尝试不成为成功领域结果，后续按明确有界策略继续或终止

#### Scenario: Stream ends without a terminal event
- **WHEN** 网络流结束但未收到有效终态
- **THEN** 保存可用审计与部分输出，并记录未知完成状态，不宣告成功

#### Scenario: Cancel after output began
- **WHEN** 用户在已收到增量后取消 Run
- **THEN** 按现有取消和 fencing 合同收场，迟到 completed 不覆盖取消结果

### Requirement: Partial audit cannot masquerade as replayable complete history

部分流内容 SHALL 可保存供审计和诊断，但只有满足协议闭合及 terminal 条件的输出才可作为普通完成结果继续消费。恢复 SHALL 保留已提交工具结果并避免盲目重放有副作用工具；无法确定执行结果时 SHALL 显式中断或要求既有恢复裁决。

#### Scenario: Crash after a tool side effect
- **WHEN** 工具可能已产生外部副作用但其输出尚未可靠提交
- **THEN** 系统不以自动重试宣称 exactly-once 执行，按不确定执行合同阻止盲目重复

### Requirement: Usage reflects actual requests and distinguishes unavailable values

使用量 SHALL 按实际模型尝试记录可用的输入、输出、reasoning 和缓存指标，缺失字段 MUST NOT 当作零。窗口估算 SHALL 针对实际 instructions、tools、schema、保留历史和材料输入，不能只计算 semantic view。重复 terminal 处理 MUST NOT 重复累计用量。

#### Scenario: World state and tool schemas consume context
- **WHEN** semantic view 很小但实际请求包含较多政策、工具和材料
- **THEN** 窗口校验基于完整实际请求并与压缩触发采用一致口径

#### Scenario: Provider omits cache usage
- **WHEN** Provider 未返回缓存使用量
- **THEN** 指标标为不可用，不报告零缓存或推断未发生缓存

### Requirement: Existing display settlement and bounded rendering are preserved

协议迁移 SHALL 保持现有消息身份、snapshot 交接、工具行、reasoning 行、幂等 reducer 和大正文渲染边界。display 隐藏内部 Items MUST NOT 隐藏需要用户处理的失败或中断状态；终态 snapshot 与已显示增量 SHALL 去重。

#### Scenario: Final snapshot follows streamed text
- **WHEN** 最终 snapshot 已包含此前显示的助手增量
- **THEN** 助手正文和执行行只出现一次，不重复追加完整 Item 文本

#### Scenario: An oversized tool result settles
- **WHEN** 工具产生很大的输出并进入权威 snapshot
- **THEN** 仍以工具行按需展开，不能退化为大量助手流式正文或解除前端体积边界
