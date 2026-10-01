## Purpose

为不同 Focus Agent 角色建立有来源和生命周期的请求上下文，使基础行为、动态运行政策、用户选定参考、冻结认知输入和协作消息分别组装及审计，并在恢复与角色隔离中保持现有材料、授权和认知边界。

## ADDED Requirements

### Requirement: Commitment handoff separates confirmed contract from frozen knowledge

Commitment SHALL 将已批准的合同与配套理论依据编译为不同 Items：合同按可信批准来源成为 task_contract，理论依据按 child artifact 的精确冻结内容／版本成为 selected_context，默认为 reference_only。MUST NOT 将混合 final_message 整段当作获批合同，MUST NOT 在恢复时用当前文件内容替代冻结引用。SHALL 复用既有第 7 阶段批准，不新增批准对话，也不因合同批准扩大工具权限。

#### Scenario: A completed commitment contains contract and theoretical references
- **WHEN** 第 9 阶段结果包含合同及多个理论依据来源
- **THEN** 合同独立保留批准证明，理论依据各自保留版本／hash／来源且为 reference_only；同处一个交付结果不传播合同批准权威

#### Scenario: A knowledge file changes after its snapshot was frozen
- **WHEN** 父流程恢复时知识文件磁盘版本与冻结 artifact 不同
- **THEN** 使用可校验的冻结内容；无法解析或证明该版本时返回来源错误，不悄悄读取当前文件替换

#### Scenario: Resume before parent handoff is committed
- **WHEN** 子流程完成并保存合同和冻结知识后，父 checkpoint 提交前失败
- **THEN** 恢复从精确子 checkpoint 编译相同身份及内容并重试交付，不重新读取可变知识或把未持久化交付标记为已完成

#### Scenario: Approval does not elevate execution access
- **WHEN** 用户批准合同中包含需要更高工具权限的工作
- **THEN** 合同只确认任务范围，实际调用仍由当前 SecurityContext 与具体调用批准裁决，子流程事件保持现有主流隔离

### Requirement: Context assembly separates behavior and scoped inputs

每次模型请求 SHALL 可区分 Base Instructions、结构化工具、WorldState、selected context、冻结 Run／Round 输入和任务 execution history。基础行为 SHALL 作为请求级 instructions，不成为任务 authored 内容或 semantic 命题；动态内容 MUST NOT 永久粘入不可追溯的大型基础 prompt。

#### Scenario: A selected skill changes between runs
- **WHEN** 用户在后续 Run 选择不同 skill
- **THEN** 新 Run 使用其版本化 selected context，基础行为不携带上一 Run 的隐式选项

#### Scenario: Replay a retained history
- **WHEN** 系统构造下一次手工 replay 请求
- **THEN** 当前适用 instructions 和 tools 仍被显式发送，历史不承担替代请求级配置的作用

### Requirement: Context is assembled according to role and ownership

Main、Teammate、Worker 和各认知角色 SHALL 只接收其职责允许的上下文。冻结 Patrol／Curator 输入、工具能力和任务来源 SHALL 保持既有隔离；短生命周期认知调用 MUST NOT 自动继承 Main 的 workspace 权限、mailbox 或完整运行历史。

#### Scenario: Invoke a semantic projector
- **WHEN** 系统调用局部语义投影角色
- **THEN** 输入为被允许的 semantic segment 与投影合同，不能同时获得 Main 的 opaque reasoning、运行授权或未冻结背景

#### Scenario: Invoke a patrol for a frozen round
- **WHEN** Patrol 开始处理本轮决策
- **THEN** 使用指定 Observation、Progress 和 Lineage 版本，不在 sampling 前替换为实时查询结果

### Requirement: Selected context bindings are immutable and attributable

selected memory、skill、material 和 task contract SHALL 绑定精确内容版本、来源和 Run／Context 作用域。Skill catalog 与显式选择的 skill 内容 SHALL 分开；材料政策与用户材料正文 SHALL 分开。模型阅读外部内容 MUST NOT 将其自述规则提升为宿主 policy 或用户授权。

#### Scenario: A selected skill file changes after admission
- **WHEN** 已绑定 Run 的 skill 文件在磁盘上被修改
- **THEN** Run 使用已冻结内容或明确报告版本缺失，不静默替换为最新内容

#### Scenario: A material contains an instruction to expand access
- **WHEN** 材料正文要求忽略权限或执行未授权行为
- **THEN** 正文保持材料来源和语义资格，不能覆盖宿主 permissions 或批准工具调用

### Requirement: Collaboration delivery is durable and deduplicated

协作消息 SHALL 保留 author、recipient、source Run／Context 和稳定 delivery identity。消息仅在进入可恢复执行上下文后才可确认投递；重试 SHALL 去重，不能仅在读取或构造 prompt 时使其永久消失。模型输入投递 MUST NOT 被表述为模型已经理解内容。

#### Scenario: Crash after reading an inbox
- **WHEN** 消息已被读取但对应执行输入未持久化就退出
- **THEN** 消息仍可在恢复时投递，不因已构造 prompt 而丢失

#### Scenario: Crash between persistence and delivery acknowledgment
- **WHEN** 消息已进入 checkpoint，但确认投递前退出
- **THEN** 恢复按 delivery identity 确认既有投递，不重复追加正文

### Requirement: Request context is auditable without another authority

每次模型尝试 SHALL 可定位所用 Revision／checkpoint、基础行为版本、selected bindings、冻结输入、WorldState 和工具／Provider 合同。审计记录 SHALL 引用这些权威来源，不建立可独立修改的另一套 Context、Progress 或 Lineage。

#### Scenario: Inspect a completed model attempt
- **WHEN** 用户或诊断消费者查询某次尝试的输入来源
- **THEN** 可确定它使用哪些精确版本及实际投影合同，不以当前实时状态冒充历史输入

### Requirement: Material and must-view behavior is preserved in actual requests

上下文重组 SHALL 保持现有材料与 must-view 合同。所需图像 SHALL 在本轮每次主模型请求中以图像内容块提供；能力不足或必需材料不可读 SHALL 可诊断；未选图像 MUST NOT 因完整历史 replay 被重新送入本轮请求。窗口估算与压缩 SHALL 使用相同实际输入口径并保持现有图片计量。

#### Scenario: Replay a history containing a previously selected image
- **WHEN** 本轮未选择该历史图像而执行手工 replay
- **THEN** 展示与来源可以保留该图像，但本轮模型请求不重新携带它

#### Scenario: Required image is removed by context rebuilding
- **WHEN** 本轮必需图像不在重建后的执行输入中
- **THEN** sampling 前按精确绑定重新提供图像，不能以文字摘要替代或静默成功
