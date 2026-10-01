## Purpose

为 Focus 的不可变 Revision 和可恢复执行历史提供 Provider 无关的 typed Item 合同，使任务定义、工具交换、运行控制和原生 continuation 可以无损保存、独立投影，并在旧版本读取、分支操作和崩溃恢复中保有正确来源及身份。

## ADDED Requirements

### Requirement: Typed items preserve identity and trusted provenance

每个历史 Item SHALL 保存稳定身份、类型、有效载荷、宿主确认的来源及生命周期作用域。语义资格和授权来源 MUST NOT 由 wire role、正文标签或模型自述决定；模型或 Curator MUST NOT 自行提升来源信任或执行权限。

#### Scenario: Runtime facts use a user wire role
- **WHEN** 环境事实投影为 user message
- **THEN** 其来源仍为运行上下文，不成为真正用户授权或可自动抽取的任务要求

#### Scenario: A delegated instruction looks like human input
- **WHEN** Patrol 委托指令与真实用户输入具有相同模型 role
- **THEN** 两者保留不同宿主 provenance，委托指令继续受现有 grant、Kernel 和新鲜度约束

### Requirement: History persistence is lossless and distinct from display

权威历史 SHALL 无损保存支持的 Items 的次序、身份、调用关联、内容块、Provider continuation、终态及必要元数据。界面展示的隐藏、折叠或字段裁剪 MUST NOT 改变持久化历史；持久化后重新读取 SHALL 能构造等价合法执行输入。

#### Scenario: Persist a reasoning and tool exchange
- **WHEN** 一次输出包含 reasoning、多个 function calls 和 assistant message，并随后收到各调用输出
- **THEN** 读取恢复保留完整顺序、独立 call IDs 和原生 continuation，display 不需要暴露全部内部载荷

#### Scenario: Preserve an unknown provider item
- **WHEN** Provider 返回尚未识别的 Item
- **THEN** 系统保存原始载荷及来源，禁止自动索引或静默当作普通正文；后续 replay 由明确兼容策略决定

### Requirement: Legacy revisions are adapted without rewriting authority

系统 SHALL 区分 V1 与 V2 history。V1 通过确定性只读适配供新消费者使用，MUST NOT 原位修改旧 Revision 内容、hash、checkpoint、来源边或既有索引。不能证明的旧 provenance SHALL 显式标记为遗留未知，MUST NOT 从正文猜测授权。新 Revision SHALL 写入 V2；未知 schema SHALL 返回可诊断错误。

#### Scenario: Read and derive from a V1 revision
- **WHEN** 用户打开 V1 Revision 并从它派生新 Context
- **THEN** 旧记录和内容 hash 不变，新 Revision 使用 V2 并保留精确来源引用与适配版本

#### Scenario: Encounter a future schema
- **WHEN** 当前程序读取无法支持的 history schema
- **THEN** 返回明确不兼容结果，不把载荷作为 V1 继续执行或覆盖

### Requirement: Authored and execution histories have separate authority

V2 authored view SHALL 只承载被作者或 Curator 明确定义的任务上下文；execution view SHALL 另行容纳合法执行协议、运行控制与必要 repair。只为执行存在的 WorldState、reasoning、repair 或 Revision boundary MUST NOT 自动成为 authored 内容。已发布 Revision MUST NOT 为持续执行原位追加；执行产生的后续 checkpoint SHALL 按现有结算规则形成新的已发布版本。

#### Scenario: Append a permissions update during a run
- **WHEN** 已发布 Revision 开始运行并追加权限状态 Item
- **THEN** 更新进入新执行 checkpoint，原 Revision 与其 authored view 保持不变

#### Scenario: Repair an authored tool fragment
- **WHEN** authored 内容缺少执行所需的工具协议片段
- **THEN** repair 具有独立来源和审计，不能伪装为真实工具执行结果或被用户 authored 的事实

### Requirement: Continuation is scoped to an eligible execution branch

Provider opaque reasoning 和 compaction continuation SHALL 绑定其 Provider、投影合同及执行分支。同一兼容分支延续 SHALL 保留必要 continuity；派生、合并、历史重写或 Provider 切换 MUST NOT 默认复制失去合法前缀的 continuation。可见 reasoning summary MUST NOT 替代原生 continuity。

#### Scenario: Continue an unchanged branch
- **WHEN** 当前 checkpoint 的前缀及 Provider 合同满足 continuation 条件
- **THEN** replay 使用保存的原生 continuation，工具交换不会仅恢复可见正文

#### Scenario: Merge two contexts
- **WHEN** 两个来源 Context 含不同的 opaque reasoning 或 Provider compaction
- **THEN** 新分支按任务语义和证据重新构建，不能拼接两套 opaque continuation 作为可运行历史

### Requirement: A compatibility bridge cannot create a second history authority

存在兼容消息表示时，系统 SHALL 保证其与 typed execution history 的对应关系可验证。无法无损映射的内容 SHALL 留在权威 typed history 并由合适执行路径处理，MUST NOT 静默丢弃、重复 append 或单独修改另一份历史继续运行。

#### Scenario: Resume through a compatibility representation
- **WHEN** 工具输出持久化后进程重启并恢复执行
- **THEN** 同一调用与输出只出现一次，恢复输入与权威 checkpoint 的 typed history 一致

#### Scenario: Detect divergent representations
- **WHEN** 兼容消息与 typed history 的调用身份或内容不一致
- **THEN** 执行被阻止并返回可诊断错误，而非任选一份作为最新历史
