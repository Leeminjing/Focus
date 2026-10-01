## Why

Focus 当前以 Chat Completions 风格消息同时承载任务历史、模型协议和界面展示，运行上下文又分散拼入 system prompt。这无法可靠表达 Responses 的独立 Items，也容易让权限、环境、reasoning 等执行信息进入语义派生；需要在保持 Context Revision、Round 冻结输入和服务端权限权威的前提下，重建上下文边界并迁移执行协议。

## What Changes

- 建立 Provider 无关的版本化 `FocusItem` 与无损历史 codec，明确 Item 类型、宿主来源、作用域和语义资格；独立生成 display、semantic、execution 投影。
- **BREAKING**：新 Revision 使用 V2 history 合同，`authored` 明确表示用户或 Curator 定义的任务上下文。V1 只读适配，不原位修改旧内容、hash、来源边或索引。
- 增加确定性 semantic view；RSI、Curator 和 Context Expansion 不再直接消费完整 execution history。工具证据保持调用关联，任务命题、证据、引用和运行控制各有明确资格，升级完整索引兼容 fingerprint。
- 引入 `FocusWorldState`，按稳定 section ID 生成完整状态或追加更新；snapshot 与精确 checkpoint、保留状态 Items 和渲染合同绑定，支持 rollback、压缩后重建和新派生分支重新初始化。
- 拆分 Base Instructions、动态 WorldState、selected context、冻结 Round 输入和 collaboration inbox；维护现有材料、must-view、Task Progress、Lineage、Kernel、租约与权限边界。
- **BREAKING**：OpenAI 与 DeepSeek 的目标执行路径改为 Responses API，统一手工 Item replay；Provider conversation 和 `previous_response_id` 不成为上下文权威。保留现有 LangGraph 调度及必要的无损 BaseMessage bridge。
- 实现 Provider 能力合同、合法 input 投影、structured output 和流事件标准化；原生 continuation 独立于可见 reasoning，显式处理 incomplete、failed、取消和断流。
- Chat Completions 仅作为显式迁移兼容入口，不允许静默 fallback；不引入 Responses hosted tools 作为 Focus 工具权威，不把 Provider compaction 当作 Focus Compression publication。

本 change 的 artifacts 完成只代表规划可审阅。用户明确同意本 change 后才可 apply；修改应用代码前必须再次成功调用 Context7，找不到或无法使用时立即停止并询问用户。

## Capabilities

### New Capabilities

- `focus-item-history`: V2 typed history、可信来源、无损 codec、旧 Revision 适配及执行分支 continuation。
- `revision-semantic-selection`: 确定性 semantic view、协议关联工具证据、引用继承和索引兼容规则。
- `checkpoint-world-state`: checkpoint 绑定的完整状态、增量更新、保留基线证明和恢复合同。
- `scoped-agent-context`: 按角色组装 Base／WorldState／selected／Round／inbox 上下文及可恢复投递。
- `responses-provider-projection`: OpenAI／DeepSeek 能力感知的手工 replay、工具及 structured output 请求投影。
- `responses-stream-lifecycle`: typed 流事件、原生 Item 收束、使用量、终态和现有展示事件衔接。

### Modified Capabilities

无。当前主 specs 只有图像与模型声明能力；本 change 不改变其用户行为要求。已有 Revision、RSI、Loop 和 stream 合同仍位于未归档 changes，作为本设计的兼容约束，不擅自同步或归档。

## Impact

- Harness：`focus/models`、`focus/agents/lead`、`focus/runtime/runs`、`focus/security/model_context.py` 及工具／模型 middleware。新增模块由 history、runtime context、provider projection 各自负责，不能继续堆入 `service.py` 或 UI codec。
- Desktop：`context_evolution`、`context_protocol.py`、`context_curator/projector.py`、`agent_loop/context_expansion`、角色工厂及 `collab.py`；所有模型调用角色须使用明确的 protocol 合同。
- 持久化：按需添加 history schema 版本、V2 载荷、精确 checkpoint 的执行上下文与投递记录；复用现有 publication、fencing 和 outbox，不建立第二套 Context／Lineage 权威。
- 依赖与配置：为 OpenAI SDK、LangChain／LangGraph 固定经合同测试的兼容范围，显式声明 provider／protocol／能力；旧配置进入可诊断兼容读取，不根据模型名猜测图像能力。
- 前端/API：保留现有 `tokens`、`reasoning`、工具行与 snapshot 交接语义，通过 display projector 隔离内部 Items；验证旧 Revision、材料和 must-view 显示及运行状态。
- 实施规范：从既有职责边界重构；文件头声明式注释说明公开接口、输入、输出、工作流和示例，其他位置仅保留必要注释；类遵守单一职责，辅助方法受保护或按用途使用静态／类方法，避免长函数与集中式巨型类。
