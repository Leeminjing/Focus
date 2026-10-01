## Purpose

定义 RSI、Curator 和 Context Expansion 可消费的任务语义边界，使真正任务命题、关联工具证据、已选引用与仅用于模型执行的运行信息各有明确资格，同时保持不可变 Revision 的来源证明、跨段解释和索引缓存兼容性。

## ADDED Requirements

### Requirement: Semantic history is a deterministic derived view

系统 SHALL 根据精确 Revision、可信来源和版本化 selection policy 生成 semantic view。semantic view MUST NOT 成为独立可编辑的第四套历史；RSI、Curator 和派生消费者 SHALL 使用同一语义选择合同，不直接把完整 execution history 当作任务事实输入。

#### Scenario: Re-read the same semantic view
- **WHEN** 两次读取相同 Revision、来源绑定和 selection policy 版本
- **THEN** 返回相同稳定 Item 身份、内容和投影 hash

#### Scenario: Attempt independent semantic editing
- **WHEN** 调用方请求只修改 semantic view 而不创建任务历史 Revision
- **THEN** 系统拒绝该写入，不出现与 Revision 不一致的语义真相

### Requirement: Selection eligibility applies across the entire indexing pipeline

语义资格 SHALL 区分 index、evidence_only、reference_only 和 exclude。index 允许抽取命题但 MUST NOT 自动表示已确认；evidence_only 不独立生成 fallback 命题；reference_only 提供可追溯解释或约束引用；exclude 不进入任务语义。分段、fallback、coverage、grounding、跨段解释和 Curator 源投影 SHALL 遵守这些资格。

#### Scenario: World state changes without new task content
- **WHEN** execution history 仅新增权限、环境、catalog 或 agent mode 更新
- **THEN** 不产生相应任务命题或 fallback hypothesis，不扩大任务语义覆盖分母

#### Scenario: An assistant claims an unverified result
- **WHEN** assistant 结论具有 index 资格但没有充分证据
- **THEN** 系统保留原有 hypothesis、verification 或 rejection 区分，不因资格而认定为事实

### Requirement: Tool evidence retains its invocation context

进入 semantic view 的工具证据 SHALL 保留工具身份、调用参数、调用 ID、输出状态及精确来源关系。排除工具调用作为任务命题 MUST NOT 使输出失去解释上下文；仅有孤立输出或协议 repair 的内容 MUST NOT 冒充完整已执行证据。

#### Scenario: Select a search result as evidence
- **WHEN** 检索输出被选为 evidence_only
- **THEN** 语义消费者可以识别对应查询与调用来源，且不会把调用动作本身当成用户任务要求

#### Scenario: Encounter an orphan tool result
- **WHEN** 所选历史不能证明工具输出对应的真实调用
- **THEN** 系统标记证据不完整并执行既有拒绝或修复审计规则，不凭空补出成功执行

### Requirement: Selected references remain available without repetitive extraction

selected memory、skill 或 material 中的任务相关约束 SHALL 通过版本化引用保持可解析性，避免重复抽取 MUST NOT 让约束静默消失。派生 SHALL 明确绑定的继承、重新选择或排除，并保留用户选择来源；平台 catalog 和运行指导 MUST NOT 因使用相同内容格式而成为任务约束。

#### Scenario: Derive a context with a selected memory constraint
- **WHEN** 来源 Context 已绑定用户选定的部署审核约束，并且派生声明继承该绑定
- **THEN** 新 Context 能解析该精确约束及来源，不需要把整份 memory 再生成一组重复任务命题

#### Scenario: A reference is no longer available
- **WHEN** 被继承的精确引用已不可解析
- **THEN** 返回可诊断的缺失来源结果，不偷偷使用当前文件的新版本替代

### Requirement: Index compatibility includes semantic selection contracts

索引缓存身份及增量继承证明 SHALL 包含 history adapter、semantic selection、分段、局部投影和跨段解释合同版本，以及其实际输入来源。版本或输入不兼容 SHALL 冷构建新索引，MUST NOT 覆盖旧记录；运行控制变化不得伪装成任务语义新增。

#### Scenario: Enable V2 semantic selection
- **WHEN** 相同旧 Revision 首次按新的 semantic selection 合同建立索引
- **THEN** 不命中仅适用于旧完整消息输入的缓存，旧索引仍保留

#### Scenario: Reuse an unchanged eligible prefix
- **WHEN** 追加 Revision 的任务语义前缀、证据依赖和版本均满足现有增量证明
- **THEN** 复用兼容局部证据并重绑定到新来源；有语义变化时整体解释仍按既有合同重算

### Requirement: Semantic evidence remains resolvable to authoritative sources

过滤执行 Items 后，所有 semantic unit、quote 和 citation SHALL 仍能解析到精确 Revision、checkpoint 和原始 Item 身份。MUST NOT 将过滤后的位置直接冒充原始消息 ordinal；多源派生 SHALL 保留来源隔离。

#### Scenario: Resolve a quote after runtime items were excluded
- **WHEN** 原始历史中任务消息之间含有多个被排除的运行控制 Items
- **THEN** quote 仍解析到原始正确内容，不能引用过滤前后位置不同的其他消息
