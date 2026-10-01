# Implementation verification

实施授权：用户在 artifacts 完成后显式调用 openspec-apply-change。Context7 preflight 在改应用源码之前完成，详见 implementation-preflight.md。

## 实施结果

FocusItem/V2 authority、四种 view、semantic selection、checkpoint-bound WorldState、Run 冻结上下文、耐久 inbox、双 Provider 手工 replay、完整 SSE 校验及尝试/工具恢复 ledger 已接入。默认配置明确 DeepSeek Responses；固定旧 adapter 继续解释 legacy Chat。依赖固定 LangChain 1.3.10、core 1.4.7、LangGraph 1.2.6、langchain-openai 1.3.2、OpenAI 2.43.0，正式迁移 head 为 c25e6f7a8b9c。

## 已完成的可重复验证

- 核心 codec/WorldState/typed/inbox/Responses/role/cognitive/attempt 套件：76 项通过；唯一测试失败是断言权限说明不能提及其它模式，已改为直接检查 access_mode 后单独复核。
- 角色与恢复集成：31 项通过，包含 Main/Teammate/Worker 实际 HTTP payload、独立 Structured Worker、精确预算、维护准入、PostgreSQL completed-result 崩溃恢复与递归 Context 派生。
- Patrol/Memory/默认 Responses 子集：48 项通过；同一个 live-mode 断言修正另行复核。
- 桌面端 51 个 *.test.cjs 文件全部退出 0，覆盖 streaming、图片/must-view、压缩缩略图、长正文/DOM 边界、模型设置和 Loop live 视图；另外 Live Loop/collapse 两个 .test.js 共 34 项通过。未运行人工交互或真实 Electron 窗口 E2E，不将 Node/后端 API 合同称为该验证。
- 114 个修改/新增 Python 文件通过语法与声明式头部检查；Harness history/context 不反向导入 Desktop ORM。
- 全量后端：1584 项通过、20 项失败、6 项跳过（829.89 秒）。13 个失败与独立 HEAD 基线完全一致；其余 7 个为新增准入配置夹具、协议私有方法、头部与 live-mode 断言，修正后相关整组 55 项通过（20.31 秒）。不把全量结果写成全绿，也不简单将重复专项通过数累加为全量通过数。
- 客户端/模型设置离线套件 86 项通过（38.40 秒），随后新增的延迟初始化用例在上述 55 项中通过。最后 semantic/Curator 全组 32 项通过（13.28 秒），覆盖独立 semantic 写入拒绝、精确引用继承和缺失来源诊断。
- OpenSpec strict 校验与 git diff --check 通过。

测试命令以仓库根为 cwd，设置 PYTHONPATH=backend/packages/harness：

```powershell
python -m pytest backend/tests -q -p no:cacheprovider --tb=short
python -m pytest backend/tests/test_access_mode_equipment.py backend/tests/test_agent_loop_architecture.py backend/tests/test_context_curator_contract.py backend/tests/test_live_sandbox_mode.py backend/tests/test_swarm_runtime_regressions.py backend/tests/test_responses_role_cutover.py -q -p no:cacheprovider --tb=short
python -m pytest backend/tests/test_focus_item_history.py backend/tests/test_typed_history_checkpoint.py backend/tests/test_checkpoint_world_state.py backend/tests/test_scoped_context_inbox.py backend/tests/test_responses_protocol.py backend/tests/test_responses_role_cutover.py backend/tests/test_execution_attempt_audit.py backend/tests/test_live_sandbox_mode.py backend/tests/test_patrol_cognitive_contract.py backend/tests/test_patrol_session.py -q -p no:cacheprovider --tb=short
node --test desktop/loop-live-projection.test.js desktop/map-collapsible-view.test.js
openspec validate migrate-focus-context-to-responses --strict
```

测试数据库由 fixture 创建；DDL/回填用例使用独立随机测试库。没有以用户数据库作为降级或故障注入目标。

## 真实 Provider smoke

| 项目 | DeepSeek | OpenAI |
|---|---|---|
| function 工具回合、manual replay | 已通过 | 离线 HTTP 已通过；live 未运行 |
| text.format JSON schema | 已通过 | 离线 HTTP 已通过；live 未运行 |
| reasoning stream、原生 item 保存 | 已通过 | 离线 SSE 已通过；live 未运行 |
| reasoning continuation replay | 已通过 | 离线 encrypted replay 已通过；live 未运行 |
| 图片 | Vision Exp 16×16 红图通过 | 显式能力/role 离线合同通过；live 未运行 |

结果见 live-smoke.json、live-reasoning-replay.json、live-image.json。没有独立 OpenAI Provider 凭据，因此没有将 DeepSeek 兼容端点冒充 OpenAI live。真实 high reasoning + forced function 返回 400；auto reasoning 工具回合已通过，已加入发送前显式组合校验与架构文档。报告不包含密钥或原生敏感 reasoning。

## 既有失败的独立对照

在 .tmp/responses-head-baseline 使用 git archive HEAD 形成未修改源码副本，以相同安装依赖复跑失败子集：77 项通过、13 项失败（21.10 秒）。该组包括 authority surfaces、assembly config、旧 facts/metrics、governed-key 手工夹具、MCP trust、spatial containment、旧 projection cursor，以及未播种 DesktopRun 的 live activity FK。没有修改生产权限或领域事实来迎合旧断言。test-results.json 保存逐项基线失败名称、7 个已修正测试和复核结果。

独立 v5 golden 也由该 HEAD 的 RevisionSemanticIndexer 生成，保存于 backend/tests/fixtures/responses/legacy-v5-index.json。旧 index ID 为 7be7ed7fe4a2c418a0109276037031e05fb7aebbb76e1da66f79788aee554606；新 v6 不复用该身份。

## 六份 specs 的场景验收映射

以下每条 Scenario 对应实际离线/数据库/显示合同套件。scenario-coverage.json 进一步列出 69 个场景的具体测试函数；该映射是组件合同证据，不将单个测试宣称为完整外部服务端到端证明。真实 OpenAI 服务能力仍按上表标为未验证。

| Spec / Scenario | 验收入口 |
|---|---|
| checkpoint-world-state — State is unchanged at the next sampling | test_checkpoint_world_state.py; test_scoped_context_inbox.py; test_execution_attempt_audit.py; test_responses_role_cutover.py; test_live_sandbox_mode.py |
| checkpoint-world-state — A catalog entry is removed | test_checkpoint_world_state.py; test_scoped_context_inbox.py; test_execution_attempt_audit.py; test_responses_role_cutover.py; test_live_sandbox_mode.py |
| checkpoint-world-state — Compression removes a permissions fragment | test_checkpoint_world_state.py; test_scoped_context_inbox.py; test_execution_attempt_audit.py; test_responses_role_cutover.py; test_live_sandbox_mode.py |
| checkpoint-world-state — Rendering contract changes | test_checkpoint_world_state.py; test_scoped_context_inbox.py; test_execution_attempt_audit.py; test_responses_role_cutover.py; test_live_sandbox_mode.py |
| checkpoint-world-state — Crash after preparing a request | test_checkpoint_world_state.py; test_scoped_context_inbox.py; test_execution_attempt_audit.py; test_responses_role_cutover.py; test_live_sandbox_mode.py |
| checkpoint-world-state — Fail before checkpoint persistence | test_checkpoint_world_state.py; test_scoped_context_inbox.py; test_execution_attempt_audit.py; test_responses_role_cutover.py; test_live_sandbox_mode.py |
| checkpoint-world-state — Roll back after access mode was narrowed | test_checkpoint_world_state.py; test_scoped_context_inbox.py; test_execution_attempt_audit.py; test_responses_role_cutover.py; test_live_sandbox_mode.py |
| checkpoint-world-state — Derive into another workspace | test_checkpoint_world_state.py; test_scoped_context_inbox.py; test_execution_attempt_audit.py; test_responses_role_cutover.py; test_live_sandbox_mode.py |
| checkpoint-world-state — Permission changes between sampling and tool execution | test_checkpoint_world_state.py; test_scoped_context_inbox.py; test_execution_attempt_audit.py; test_responses_role_cutover.py; test_live_sandbox_mode.py |
| checkpoint-world-state — Approve one specific tool call | test_checkpoint_world_state.py; test_scoped_context_inbox.py; test_execution_attempt_audit.py; test_responses_role_cutover.py; test_live_sandbox_mode.py |
| checkpoint-world-state — Progress is published while a round is running | test_checkpoint_world_state.py; test_scoped_context_inbox.py; test_execution_attempt_audit.py; test_responses_role_cutover.py; test_live_sandbox_mode.py |
| focus-item-history — Runtime facts use a user wire role | test_focus_item_history.py; test_typed_history_checkpoint.py; test_focus_history_migration.py; test_recursive_context_forking.py |
| focus-item-history — A delegated instruction looks like human input | test_focus_item_history.py; test_typed_history_checkpoint.py; test_focus_history_migration.py; test_recursive_context_forking.py |
| focus-item-history — Persist a reasoning and tool exchange | test_focus_item_history.py; test_typed_history_checkpoint.py; test_focus_history_migration.py; test_recursive_context_forking.py |
| focus-item-history — Preserve an unknown provider item | test_focus_item_history.py; test_typed_history_checkpoint.py; test_focus_history_migration.py; test_recursive_context_forking.py |
| focus-item-history — Read and derive from a V1 revision | test_focus_item_history.py; test_typed_history_checkpoint.py; test_focus_history_migration.py; test_recursive_context_forking.py |
| focus-item-history — Encounter a future schema | test_focus_item_history.py; test_typed_history_checkpoint.py; test_focus_history_migration.py; test_recursive_context_forking.py |
| focus-item-history — Append a permissions update during a run | test_focus_item_history.py; test_typed_history_checkpoint.py; test_focus_history_migration.py; test_recursive_context_forking.py |
| focus-item-history — Repair an authored tool fragment | test_focus_item_history.py; test_typed_history_checkpoint.py; test_focus_history_migration.py; test_recursive_context_forking.py |
| focus-item-history — Continue an unchanged branch | test_focus_item_history.py; test_typed_history_checkpoint.py; test_focus_history_migration.py; test_recursive_context_forking.py |
| focus-item-history — Merge two contexts | test_focus_item_history.py; test_typed_history_checkpoint.py; test_focus_history_migration.py; test_recursive_context_forking.py |
| focus-item-history — Resume through a compatibility representation | test_focus_item_history.py; test_typed_history_checkpoint.py; test_focus_history_migration.py; test_recursive_context_forking.py |
| focus-item-history — Detect divergent representations | test_focus_item_history.py; test_typed_history_checkpoint.py; test_focus_history_migration.py; test_recursive_context_forking.py |
| responses-provider-projection — Responses capability is unavailable | test_responses_protocol.py; test_responses_role_cutover.py; test_model_settings.py; test_model_settings_api.py |
| responses-provider-projection — An old configuration is loaded | test_responses_protocol.py; test_responses_role_cutover.py; test_model_settings.py; test_model_settings_api.py |
| responses-provider-projection — Continue on OpenAI | test_responses_protocol.py; test_responses_role_cutover.py; test_model_settings.py; test_model_settings_api.py |
| responses-provider-projection — Continue on DeepSeek | test_responses_protocol.py; test_responses_role_cutover.py; test_model_settings.py; test_model_settings_api.py |
| responses-provider-projection — Project policy to DeepSeek | test_responses_protocol.py; test_responses_role_cutover.py; test_model_settings.py; test_model_settings_api.py |
| responses-provider-projection — Project selected external content | test_responses_protocol.py; test_responses_role_cutover.py; test_model_settings.py; test_model_settings_api.py |
| responses-provider-projection — Receive parallel function calls | test_responses_protocol.py; test_responses_role_cutover.py; test_model_settings.py; test_model_settings_api.py |
| responses-provider-projection — Request an unsupported custom tool | test_responses_protocol.py; test_responses_role_cutover.py; test_model_settings.py; test_model_settings_api.py |
| responses-provider-projection — Run a curator structured response | test_responses_protocol.py; test_responses_role_cutover.py; test_model_settings.py; test_model_settings_api.py |
| responses-provider-projection — Required image uses an unsupported role | test_responses_protocol.py; test_responses_role_cutover.py; test_model_settings.py; test_model_settings_api.py |
| responses-provider-projection — Receive a provider compaction item | test_responses_protocol.py; test_responses_role_cutover.py; test_model_settings.py; test_model_settings_api.py |
| responses-provider-projection — Invoke a cognitive worker after protocol migration | test_responses_protocol.py; test_responses_role_cutover.py; test_model_settings.py; test_model_settings_api.py |
| responses-stream-lifecycle — Interleave reasoning and visible text | test_responses_protocol.py; test_execution_attempt_audit.py; test_must_view_images.py; desktop/conversation-*.test.cjs; desktop/run-ui.test.cjs |
| responses-stream-lifecycle — Receive tool arguments or tool output | test_responses_protocol.py; test_execution_attempt_audit.py; test_must_view_images.py; desktop/conversation-*.test.cjs; desktop/run-ui.test.cjs |
| responses-stream-lifecycle — Disconnect during arguments | test_responses_protocol.py; test_execution_attempt_audit.py; test_must_view_images.py; desktop/conversation-*.test.cjs; desktop/run-ui.test.cjs |
| responses-stream-lifecycle — Repeat an item completion event | test_responses_protocol.py; test_execution_attempt_audit.py; test_must_view_images.py; desktop/conversation-*.test.cjs; desktop/run-ui.test.cjs |
| responses-stream-lifecycle — Receive incomplete output with valid JSON prefix | test_responses_protocol.py; test_execution_attempt_audit.py; test_must_view_images.py; desktop/conversation-*.test.cjs; desktop/run-ui.test.cjs |
| responses-stream-lifecycle — Stream ends without a terminal event | test_responses_protocol.py; test_execution_attempt_audit.py; test_must_view_images.py; desktop/conversation-*.test.cjs; desktop/run-ui.test.cjs |
| responses-stream-lifecycle — Cancel after output began | test_responses_protocol.py; test_execution_attempt_audit.py; test_must_view_images.py; desktop/conversation-*.test.cjs; desktop/run-ui.test.cjs |
| responses-stream-lifecycle — Crash after a tool side effect | test_responses_protocol.py; test_execution_attempt_audit.py; test_must_view_images.py; desktop/conversation-*.test.cjs; desktop/run-ui.test.cjs |
| responses-stream-lifecycle — World state and tool schemas consume context | test_responses_protocol.py; test_execution_attempt_audit.py; test_must_view_images.py; desktop/conversation-*.test.cjs; desktop/run-ui.test.cjs |
| responses-stream-lifecycle — Provider omits cache usage | test_responses_protocol.py; test_execution_attempt_audit.py; test_must_view_images.py; desktop/conversation-*.test.cjs; desktop/run-ui.test.cjs |
| responses-stream-lifecycle — Final snapshot follows streamed text | test_responses_protocol.py; test_execution_attempt_audit.py; test_must_view_images.py; desktop/conversation-*.test.cjs; desktop/run-ui.test.cjs |
| responses-stream-lifecycle — An oversized tool result settles | test_responses_protocol.py; test_execution_attempt_audit.py; test_must_view_images.py; desktop/conversation-*.test.cjs; desktop/run-ui.test.cjs |
| revision-semantic-selection — Re-read the same semantic view | test_typed_history_checkpoint.py; test_semantic_index_cache_compatibility.py; test_incremental_index_recovery.py; test_responses_role_cutover.py; test_semantic_evidence_resolver.py |
| revision-semantic-selection — Attempt independent semantic editing | test_typed_history_checkpoint.py; test_semantic_index_cache_compatibility.py; test_incremental_index_recovery.py; test_responses_role_cutover.py; test_semantic_evidence_resolver.py |
| revision-semantic-selection — World state changes without new task content | test_typed_history_checkpoint.py; test_semantic_index_cache_compatibility.py; test_incremental_index_recovery.py; test_responses_role_cutover.py; test_semantic_evidence_resolver.py |
| revision-semantic-selection — An assistant claims an unverified result | test_typed_history_checkpoint.py; test_semantic_index_cache_compatibility.py; test_incremental_index_recovery.py; test_responses_role_cutover.py; test_semantic_evidence_resolver.py |
| revision-semantic-selection — Select a search result as evidence | test_typed_history_checkpoint.py; test_semantic_index_cache_compatibility.py; test_incremental_index_recovery.py; test_responses_role_cutover.py; test_semantic_evidence_resolver.py |
| revision-semantic-selection — Encounter an orphan tool result | test_typed_history_checkpoint.py; test_semantic_index_cache_compatibility.py; test_incremental_index_recovery.py; test_responses_role_cutover.py; test_semantic_evidence_resolver.py |
| revision-semantic-selection — Derive a context with a selected memory constraint | test_typed_history_checkpoint.py; test_semantic_index_cache_compatibility.py; test_incremental_index_recovery.py; test_responses_role_cutover.py; test_semantic_evidence_resolver.py |
| revision-semantic-selection — A reference is no longer available | test_typed_history_checkpoint.py; test_semantic_index_cache_compatibility.py; test_incremental_index_recovery.py; test_responses_role_cutover.py; test_semantic_evidence_resolver.py |
| revision-semantic-selection — Enable V2 semantic selection | test_typed_history_checkpoint.py; test_semantic_index_cache_compatibility.py; test_incremental_index_recovery.py; test_responses_role_cutover.py; test_semantic_evidence_resolver.py |
| revision-semantic-selection — Reuse an unchanged eligible prefix | test_typed_history_checkpoint.py; test_semantic_index_cache_compatibility.py; test_incremental_index_recovery.py; test_responses_role_cutover.py; test_semantic_evidence_resolver.py |
| revision-semantic-selection — Resolve a quote after runtime items were excluded | test_typed_history_checkpoint.py; test_semantic_index_cache_compatibility.py; test_incremental_index_recovery.py; test_responses_role_cutover.py; test_semantic_evidence_resolver.py |
| scoped-agent-context — A selected skill changes between runs | test_scoped_context_inbox.py; test_execution_attempt_audit.py; test_responses_role_cutover.py; test_must_view_images.py; test_subagent_assembly.py; test_memory_library.py |
| scoped-agent-context — Replay a retained history | test_scoped_context_inbox.py; test_execution_attempt_audit.py; test_responses_role_cutover.py; test_must_view_images.py; test_subagent_assembly.py; test_memory_library.py |
| scoped-agent-context — Invoke a semantic projector | test_scoped_context_inbox.py; test_execution_attempt_audit.py; test_responses_role_cutover.py; test_must_view_images.py; test_subagent_assembly.py; test_memory_library.py |
| scoped-agent-context — Invoke a patrol for a frozen round | test_scoped_context_inbox.py; test_execution_attempt_audit.py; test_responses_role_cutover.py; test_must_view_images.py; test_subagent_assembly.py; test_memory_library.py |
| scoped-agent-context — A selected skill file changes after admission | test_scoped_context_inbox.py; test_execution_attempt_audit.py; test_responses_role_cutover.py; test_must_view_images.py; test_subagent_assembly.py; test_memory_library.py |
| scoped-agent-context — A material contains an instruction to expand access | test_scoped_context_inbox.py; test_execution_attempt_audit.py; test_responses_role_cutover.py; test_must_view_images.py; test_subagent_assembly.py; test_memory_library.py |
| scoped-agent-context — Crash after reading an inbox | test_scoped_context_inbox.py; test_execution_attempt_audit.py; test_responses_role_cutover.py; test_must_view_images.py; test_subagent_assembly.py; test_memory_library.py |
| scoped-agent-context — Crash between persistence and delivery acknowledgment | test_scoped_context_inbox.py; test_execution_attempt_audit.py; test_responses_role_cutover.py; test_must_view_images.py; test_subagent_assembly.py; test_memory_library.py |
| scoped-agent-context — Inspect a completed model attempt | test_scoped_context_inbox.py; test_execution_attempt_audit.py; test_responses_role_cutover.py; test_must_view_images.py; test_subagent_assembly.py; test_memory_library.py |
| scoped-agent-context — Replay a history containing a previously selected image | test_scoped_context_inbox.py; test_execution_attempt_audit.py; test_responses_role_cutover.py; test_must_view_images.py; test_subagent_assembly.py; test_memory_library.py |
| scoped-agent-context — Required image is removed by context rebuilding | test_scoped_context_inbox.py; test_execution_attempt_audit.py; test_responses_role_cutover.py; test_must_view_images.py; test_subagent_assembly.py; test_memory_library.py |

## 运维与保存范围

维护/回退流程见 docs/focus-responses-context.md。停止新 admission 后继续保留 V2 reader/schema；显式 legacy 回退经过新 execution branch，剥离 opaque continuity；V2/receipt/attempt 存在时 destructive downgrade 被拒绝。没有原位迁移不可变 V1 或删除旧 index。

OpenSpec 目录当前被既有 .gitignore 规则忽略。Artifacts、smoke 和本验收文件已在本地保存，尚未提交；版本管理时需由项目显式纳入。应用与测试改动也未 commit/push。
