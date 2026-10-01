## Purpose

定义动态运行状态如何以完整初始化和追加更新进入模型执行历史，并让状态基线随精确 checkpoint 恢复、失效和重建，保证权限、环境与能力变化被正确表达且不会通过派生或压缩造成旧状态泄漏和更新遗漏。

## ADDED Requirements

### Requirement: World state uses stable sections with explicit update semantics

动态运行状态 SHALL 按稳定 section ID 和明确的 full replacement 或 added／removed 语义表达。首次执行 SHALL 注入适用的完整状态；兼容基线未变时不重复追加；变更时 SHALL 追加可解释的更新，不修改旧历史消息。

#### Scenario: State is unchanged at the next sampling
- **WHEN** 当前所有 section 与有效保留基线一致
- **THEN** 不新增重复 WorldState Items，下一次模型请求仍包含需要 replay 的已有状态

#### Scenario: A catalog entry is removed
- **WHEN** 工具或技能 catalog 删除了已有条目
- **THEN** 更新明确表达 removal，模型不会只看到新增条目而继续依赖旧目录

### Requirement: A snapshot is valid only for its retained checkpoint baseline

WorldState snapshot SHALL 绑定精确执行 checkpoint、所保留状态 Items 和渲染／投影合同。无法证明 retained baseline 的状态 SHALL 视为 unknown 或 absent 并完整重建相关 section。MUST NOT 用 thread ID、Revision generation 或相同状态值代替该证明。

#### Scenario: Compression removes a permissions fragment
- **WHEN** 压缩保留 snapshot 的值但删除模型输入中的权限片段
- **THEN** 下一次 sampling 前重新注入完整权限状态，不因状态值未变而跳过

#### Scenario: Rendering contract changes
- **WHEN** checkpoint 使用旧版本状态渲染或不兼容 Provider 投影
- **THEN** 对受影响 section 重建基线并记录合同版本，不直接复用旧 snapshot

### Requirement: World state and its items are recoverably committed together

状态 Items、对应 snapshot 和执行身份 SHALL 以同一可恢复 checkpoint 提交边界保存。准备请求、实际尝试和 Provider 完成状态 SHALL 可区分；仅完成组装 MUST NOT 被记录为 Provider 已完成采样。崩溃重试 MUST NOT 造成 snapshot 超前于保留历史或重复追加同一更新。

#### Scenario: Crash after preparing a request
- **WHEN** 请求上下文已持久化但网络尝试前进程退出
- **THEN** 恢复可 replay 该准备 checkpoint 的状态，且不会将该尝试标记为已完成

#### Scenario: Fail before checkpoint persistence
- **WHEN** 状态准备在持久化前失败
- **THEN** 恢复仍使用旧完整基线，不出现只更新 snapshot 的半提交状态

### Requirement: Branch operations restore or reinitialize the correct baseline

同一分支继续和 rollback SHALL 读取其精确旧 checkpoint 的基线，再与当前实际运行状态比较。新派生、合并或重建分支 SHALL 使用当前适用完整状态，MUST NOT 把父分支 runtime updates 当作可继承任务上下文。

#### Scenario: Roll back after access mode was narrowed
- **WHEN** 用户恢复曾处于 workspace-write 的旧 checkpoint，而当前实际权限为 read-only
- **THEN** 新 sampling 表达当前 read-only，旧 checkpoint 不恢复已失效的执行许可

#### Scenario: Derive into another workspace
- **WHEN** 新 Context 使用不同 workspace slot、路径或权限
- **THEN** 初始化新分支的完整相关状态，不复制父分支的 workspace 或 permission update

### Requirement: Permission descriptions never grant execution authority

permissions section SHALL 采用完整 replacement。一次具体工具调用的临时授权 MUST NOT 自动扩展为后续调用许可；工具执行 SHALL 继续依据宿主当前 SecurityContext、capabilities 和授权记录裁决，不能依据模型可见 snapshot 或其文字自述放行。

#### Scenario: Permission changes between sampling and tool execution
- **WHEN** 模型已看到较宽权限，但执行工具前宿主收窄权限
- **THEN** 工具按当前实际许可裁决，旧提示内容不能越权

#### Scenario: Approve one specific tool call
- **WHEN** 用户批准单个需要升级的工具调用
- **THEN** 授权作用域仍只覆盖该调用，不增加永久 catalog 权限或已批准命令前缀

### Requirement: Frozen round inputs are not dynamic world state

Observation、Task Progress、Committed Lineage 和本轮决策输入 SHALL 使用现有冻结版本。WorldState 刷新 MUST NOT 替换它们或把后台新版本注入同一 Round 的决策输入。

#### Scenario: Progress is published while a round is running
- **WHEN** 后台产生新 Task Progress 而本轮输入已冻结
- **THEN** 本轮认知消费者继续使用原版本，新版本只按后继 Round 合同消费
