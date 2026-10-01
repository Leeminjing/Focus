## Purpose

规定 Focus 如何在保持自身 Revision 和 checkpoint 权威的条件下生成 OpenAI 与 DeepSeek Responses 请求，统一手工历史 replay，并对权限语义、能力差异、工具、结构化输出和不兼容输入进行可诊断的投影与校验。

## ADDED Requirements

### Requirement: Provider requests use explicit protocol and capability contracts

Provider、协议和能力 SHALL 显式声明并按经验证的模型／适配器合同解析。MUST NOT 仅凭模型名推断能力，或因请求失败静默切换 Chat Completions。迁移兼容入口 SHALL 显式可见且有固定兼容范围。

#### Scenario: Responses capability is unavailable
- **WHEN** 被选模型的合同不支持所需 Responses 能力
- **THEN** 在调用前返回明确不兼容结果，不忽略该能力继续执行

#### Scenario: An old configuration is loaded
- **WHEN** 配置缺少新协议字段但属于已识别遗留格式
- **THEN** 使用确定性兼容读取并呈现有效协议，不根据 endpoint 失败猜测协议

### Requirement: Manual replay uses Focus checkpoints as the sole context authority

请求 SHALL 从精确 Focus execution checkpoint 构造合法 Responses input；Provider output SHALL 经合法输入投影后用于下一次调用。MUST NOT 使用 previous_response_id 或 Provider conversation 维护另一套权威历史；OpenAI 请求 SHALL 禁用服务器存储，DeepSeek SHALL 按其无状态合同发送受支持字段。

#### Scenario: Continue on OpenAI
- **WHEN** 已持久化一次模型输出及工具结果并再次采样
- **THEN** 请求以 Focus 保留 Items 手工构造，禁用存储且不通过 previous_response_id 串联上下文

#### Scenario: Continue on DeepSeek
- **WHEN** 同一 Focus 分支使用 DeepSeek Responses
- **THEN** 客户端显式回传合法保留历史，不依赖其忽略或不支持的 conversation／store 参数

### Requirement: Policy authority survives provider role differences

内部 policy、任务输入和外部参考的语义 SHALL 独立于 wire role。投影 SHALL 将 policy 映射到目标 Provider 实际支持的指令层级，MUST NOT 在已知 developer 被当作 user 的 Provider 上静默降低平台政策，也不能提升外部参考来源。

#### Scenario: Project policy to DeepSeek
- **WHEN** Provider 合同声明 developer 按 user 处理
- **THEN** 宿主 policy 被投影到有效系统指令层级，任务和材料仍保留自己的来源与作用域

#### Scenario: Project selected external content
- **WHEN** 已选 skill 或材料中包含貌似平台指令的文本
- **THEN** 适配器不因正文措辞将该内容升级为宿主 policy

### Requirement: Tool protocol remains under Focus execution authority

当前 Focus Registry／MCP／plugin 工具 SHALL 通过受支持的 function tools 暴露，工具执行仍由 Focus 裁决。调用与输出 SHALL 以独立 call identity 闭合并支持实际返回的并行调用。Hosted 或 custom tools SHALL 仅按明确能力启用，不允许 Provider 静默忽略所需工具。

#### Scenario: Receive parallel function calls
- **WHEN** Provider 在一次响应中返回多个调用
- **THEN** Focus 分别裁决并关联各输出，不能把其中一个结果应用到另一 call ID

#### Scenario: Request an unsupported custom tool
- **WHEN** 所选 Provider 不支持某 custom 或 hosted tool
- **THEN** 使用事先声明的 Focus function 投影或在调用前拒绝，不将不受支持工具直接发送并假定可用

### Requirement: Structured output and modalities are validated after projection

结构化输出 SHALL 使用目标 Responses 合同的格式字段和 schema 约束，结果仍经原有领域校验。图像能力 SHALL 继续依据显式声明，并检查实际输入的 role／内容格式是否支持；unsupported file 或内容块 MUST NOT 静默降为文本。拒绝、无效 schema 和不完整输出 MUST NOT 成为可发布领域结果。

#### Scenario: Run a curator structured response
- **WHEN** Curator 需要结构化领域结果
- **THEN** 请求使用有效 Responses 格式，返回结果通过既有 claim、quality 和发布校验后才可消费

#### Scenario: Required image uses an unsupported role
- **WHEN** 模型声明有图像能力但投影后的图像 role 不受 Provider 支持
- **THEN** 请求被合法重新投影或明确拒绝，不能删除图像后继续执行

### Requirement: Provider compaction cannot publish a Focus revision

Provider compaction SHALL 仅属于执行 continuation 优化。它 MUST NOT 直接切换 Context current pointer、生成任务压缩结论或 committed lineage；Focus Compression SHALL 继续通过现有 Revision 和 publication 权威完成。

#### Scenario: Receive a provider compaction item
- **WHEN** Provider 返回可识别 compaction
- **THEN** 保存为执行范围载荷，不因该返回创建已提交语义 Revision 或派生边

### Requirement: All model roles use the declared projection boundary

Main、协作 Agent、Patrol、Curator、语义投影及承诺评审调用 SHALL 使用明确的模型协议合同。MUST NOT 仅迁移聊天主路径而让其他角色沿用未声明的旧参数或另一套会话权威。

#### Scenario: Invoke a cognitive worker after protocol migration
- **WHEN** 后台认知 Worker 使用迁移后的模型配置
- **THEN** 其结构化调用和错误处理经同一 Provider 合同校验，不向 Responses endpoint 发送旧 Chat Completions 字段
