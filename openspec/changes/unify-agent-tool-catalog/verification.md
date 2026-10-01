# Verification — unify-agent-tool-catalog

验证日期：2026-10-01。基线为 `9be053f9cad111090eef7a90547af3b2d9d91f0f`。本 change 及用户授权的核验修复已完成，全部 18 项任务交付。最新 19 模块联合回归为 305 passed、零失败；两项原基线告警、一项测试增强建议及同类过期权限断言全部关闭。没有提交、推送、更新用户安装目录或归档。

## 实施结果

`focus/tools/catalog.py` 负责来源选择、只读绑定目录和名称冲突检查。Desktop Main 与默认 Harness 工厂共用来源选择，plugin 只由 PluginBridgeMiddleware 注入，发现池和 equipment 保留完整目录。编译器在框架名称映射之前拒绝普通工具、中间件内部和跨入口的重复名称，包含双方注册位置；相同对象也不去重。

`make_lead_agent` 按已安装框架的 middleware-first 顺序编译目录，覆盖 inbox、attempt 等后置工具提供者；原工具对象用于执行、WorldState 和压缩预算，Provider 保留终端协议检查。PluginBridgeMiddleware 捕获工具容器快照，返回列表的修改和后续 reload 不改变旧图绑定。没有引入 Harness → Desktop 依赖、Provider schema 副本或新的权限规则。

Context7 在首次实施和本轮核验修复的代码／测试修改前均成功查询，版本与依据见 [implementation-preflight.md](implementation-preflight.md)。最终新增及修改的 12 个 Python 文件完成 AST 语法、文件头接口／输入／输出／工作流／示例及 Harness 依赖方向检查；辅助函数保持模块保护形式，值对象、编译器与插件桥各自承担单一职责。`git diff --check` 通过。

## 首次实施的历史证据

测试使用 Python 3.12，PYTHONPATH 为仓库根目录和 `backend/packages/harness`。Provider 使用真实 FocusResponsesChatModel、LangGraph/create_agent 和 httpx MockTransport；不访问真实 API。Desktop 工厂回归保留非空插件发现池、真实插件桥及角色工具构建，只隔离模型传输和耐久数据库端口。

修复前运行 `pytest backend/tests/test_agent_tool_catalog.py -k 'True and openai' -q`，实际 Desktop Main 工厂在 CompressionGate 的 function_specs 校验处失败：`function tool name 重复`。失败发生在构图阶段，尚未发起模型 HTTP 或执行工具。日志：`.tmp/tool-catalog-red.txt`（1 failed，3 deselected；另有临时 pytest 缓存目录警告）。修复后同一场景纳入正式四组合参数测试并通过。测试夹具调试中重复 middleware 类名及禁用插件时工具节点存在性的错误预期均已修正，最终结果以以下完整专项为准。

以下为核验修复前的历史结果，保留原日志；当前结果在收尾核验部分。所有命令均带 `--tb=short -q` 和各自隔离的 `-o cache_dir=.tmp/pytest-tool-catalog-*`。

| 范围 | 正式测试模块 | 结果 | 日志 |
| --- | --- | --- | --- |
| 目录、插件、Commitment | test_agent_tool_catalog、test_tool_catalog_compilation、test_tool_catalog_snapshot、test_plugins、test_commitment_poc | 126 passed，17.24s；包括新增 27 个参数展开用例 | `.tmp/tool-catalog-core-final.txt` |
| Desktop 业务相邻回归 | test_desktop_poc | 11 passed，36.50s | `.tmp/tool-catalog-desktop-final.txt` |
| Responses、checkpoint、压缩、安全与恢复 | test_responses_protocol、test_responses_role_cutover、test_checkpoint_world_state、test_compression_gate、test_keyword_compression、test_access_middleware、test_tool_effects、test_mcp_trust、test_mcp_session_lifecycle、test_commitment_handoff_recovery、test_contract_history_repairs | 129 passed，2 failed，7.25s | `.tmp/tool-catalog-adjacent-final.txt` |
| 未修改 HEAD 的独立复测 | 上述失败的两个完整测试节点 | 2 failed，2.93s，错误与工作树一致 | `.tmp/tool-catalog-baseline-failures.txt` |
| 文件头、AST 与依赖方向 | 4 个应用文件、既有 test_plugins、测试支持文件及 3 个新测试文件 | 9 files checked，errors=[] | `.tmp/tool-catalog-static.json` |

独立复测通过 `git archive HEAD` 导出到 `.tmp/tool-catalog-baseline`，将 cwd 与 PYTHONPATH 指向该副本，再执行相同测试节点；没有修改当前工作树或用户安装数据。两项失败都在未修改基线上存在：

- 黄金索引测试当时名为 `test_independent_head_v5_golden_preserves_hash_and_v6_is_a_new_identity`：断言期望 v6，基线实际语义索引版本已经为 v8；当前正式节点为 `backend/tests/test_responses_role_cutover.py::test_independent_head_v5_golden_preserves_hash_and_v8_is_a_new_identity`。
- `backend/tests/test_mcp_trust.py::test_trust_override_config_is_an_authority_surface`：工作区外 extensions_config.json 的写访问期望 ASK，基线实际为 DENY。

首次 apply 没有修改这两项断言；用户随后要求「全部修复」，本轮校正并增强相应测试，未修改索引实现或执行权限实现。旧失败日志保留，不代表当前回归结果。

## Requirement / Scenario 覆盖

下表覆盖 `specs/agent-tool-catalog/spec.md` 的全部 6 个 requirements、16 个 scenarios。每个引用指向实际存在的正式测试函数；参数展开由 pytest 执行。

| Requirement | Scenario | 正式测试与断言 |
| --- | --- | --- |
| Plugin tools have a single execution injection owner | Main starts with an active demo plugin and compression enabled | `backend/tests/test_agent_tool_catalog.py::test_actual_desktop_main_plugin_and_compression`：OpenAI／DeepSeek × 压缩开关，真实 Main 一次绑定并完成工具交换；`backend/tests/test_agent_tool_catalog.py::test_packaged_demo_plugin_is_injected_once`：实际 demo_echo 插件目录一次绑定 |
| 同上 | Default execution discovers plugin and non-plugin tools | `backend/tests/test_agent_tool_catalog.py::test_default_harness_factory_keeps_non_plugin_discovery`：builtin/custom/MCP 保留，plugin 经桥一次注入 |
| 同上 | Equipment displays plugin capabilities | `backend/tests/test_agent_tool_catalog.py::test_role_factory_preserves_ordinary_tools_and_plugin_hooks`：equipment 保持插件可见 |
| Tool name conflicts fail before graph execution | Different sources expose the same name | `backend/tests/test_agent_tool_catalog.py::test_real_factory_conflicts_fail_before_http_or_tool_effects`：跨来源不同 callable／效果契约同名，双方 owner 与零 HTTP／副作用；`backend/tests/test_plugins.py::test_name_collision_rejected_before_system_tool_overwrites_plugin`：不再静默覆盖 |
| 同上 | The same object is injected twice | `backend/tests/test_tool_catalog_compilation.py::test_duplicate_declarations_report_both_owners_before_name_mapping`：同一对象普通／跨入口声明均拒绝 |
| 同上 | A middleware declaration contains an internal conflict | `backend/tests/test_tool_catalog_compilation.py::test_duplicate_declarations_report_both_owners_before_name_mapping`：中间件内部重复定位两个位置 |
| 同上 | Compression or model protocol changes | `backend/tests/test_agent_tool_catalog.py::test_real_factory_conflicts_fail_before_http_or_tool_effects`：Responses／显式 Chat × 压缩开关都在执行前失败 |
| All capability consumers agree on the effective directory | Responses request matches runtime and world state | `backend/tests/test_agent_tool_catalog.py::test_actual_desktop_main_plugin_and_compression`：独立比较真实请求、ToolNode 绑定与 WorldState 名称、顺序和规范化 schema |
| 同上 | Compression estimates a tool-bearing request | `backend/tests/test_agent_tool_catalog.py::test_actual_desktop_main_plugin_and_compression`：同一初始状态下 gate 估算等于实际 Provider payload 估算 |
| 同上 | A middleware contributes a distinct tool | `backend/tests/test_agent_tool_catalog.py::test_late_middleware_tool_is_visible_everywhere`：两种 Provider 实际调用后置 attempt 工具各一次，直接比较最终请求、ToolNode、WorldState、预算的名称、顺序与规范化 schema |
| Assembly snapshots preserve tool identity and reload isolation | Plugins are reloaded between graph constructions | `backend/tests/test_tool_catalog_snapshot.py::test_real_reload_and_disabling_affect_only_new_graphs`：真实文件插件 reload，新图更新、旧图原 callable 可执行 |
| 同上 | A directory consumer changes its returned list | `backend/tests/test_tool_catalog_snapshot.py::test_bridge_container_isolated_from_consumers_and_registry_growth`：列表修改隔离；`backend/tests/test_tool_catalog_compilation.py::test_source_selection_and_framework_order_preserve_original_bindings`：tuple 容器、原对象及 metadata 保留 |
| 同上 | A plugin is disabled for a new graph | `backend/tests/test_tool_catalog_snapshot.py::test_real_reload_and_disabling_affect_only_new_graphs`：禁用后新图无路由／广告，旧图不变 |
| Role composition preserves valid tool behavior | A unique plugin tool executes during a normal run | `backend/tests/test_agent_tool_catalog.py::test_actual_desktop_main_plugin_and_compression`：两种 Provider 原 callable 各执行一次，function_call/output 正常闭合 |
| 同上 | Different roles are assembled | `backend/tests/test_agent_tool_catalog.py::test_role_factory_preserves_ordinary_tools_and_plugin_hooks`：四角色实际图能力保留，plugin 和 before_model hook 各一次 |
| Tool visibility does not expand execution authority | An advertised tool requires write access | `backend/tests/test_tool_catalog_snapshot.py::test_advertised_write_tool_retains_effect_and_is_denied_in_read_only_run`：可信 effect 保留、FILE_POLICY_DENIED、零写副作用；`backend/tests/test_tool_catalog_snapshot.py::test_plugin_runtime_argument_remains_injected_and_hidden_from_model`：runtime 注入与原对象身份保留 |

补充：`backend/tests/test_tool_catalog_compilation.py::test_legacy_raw_discovery_is_not_classified_by_its_name` 验证 legacy BaseTool 来源不通过名称猜测。真实框架已安装版本及顺序由 preflight 固定，本 change 不新增依赖。

## 验证边界

没有运行完整全仓测试、真实 OpenAI／DeepSeek HTTP 或 Electron 交互 E2E；没有将当前源码部署到 `.focus/app`。离线测试验证实际工厂、模型协议投影及工具执行链路，不代表外部服务可用性。已发现的失败均已修复，最新联合回归无失败。

最终验收：`openspec validate unify-agent-tool-catalog --strict` 与 `git diff --check` 通过。全部 6 项 requirements、16 个 scenarios 映射及当前正式测试函数引用由 AST 定位；收尾证据为 `.tmp/tool-catalog-repair-inventory.json` 和 `.tmp/tool-catalog-repair-static.json`。

## OpenSpec verify-change 与修复收尾

首次独立核验重新读取 proposal、design、specs 与 tasks，检查真实装配与执行链路，并重跑 27 个新增目录用例（12.02s，`.tmp/tool-catalog-verify.txt`），发现两项基线告警和一项后置工具请求验证建议。用户明确授权全部修复后，重新成功查询 Context7，追加 tasks 6.1–6.3 并完成下列修复。

| 修复项 | 正式证据 |
| --- | --- |
| 旧 v5 黄金索引与当前 v8 身份 | `backend/tests/test_responses_role_cutover.py::test_independent_head_v5_golden_preserves_hash_and_v8_is_a_new_identity`：固定旧黄金哈希、旧版本可读、旧文件字节不变、当前版本明确为 v8、来源保持一致、新身份不同且序列化后可重新校验 |
| 配置权柄面与文件模式边界 | `backend/tests/test_mcp_trust.py::test_trust_override_config_is_an_authority_surface`：真实分层来源，临时工作区与独立全局目录，三档模式分别验证内外配置的读写；只读写入 DENY、工作区内敏感写入 ASK、根外写入 DENY、完全访问敏感写入仍 ASK |
| 五项同类旧权限断言 | `backend/tests/test_authority_surfaces.py::test_read_and_write_are_decided_separately`、`backend/tests/test_authority_surfaces.py::test_full_mode_lifts_file_boundary_but_preserves_authority_review`、`backend/tests/test_authority_surfaces.py::test_missing_mode_defaults_to_read_only`、`backend/tests/test_authority_surfaces.py::test_unrecognised_mode_defaults_to_read_only_with_a_warning`、`backend/tests/test_authority_surfaces.py::test_access_mode_does_not_change_the_tool_set`：敏感读写分离、完全访问保留业务审批、缺省／未知值只读、外部普通读取可放行、受限 Shell 常规调用不等于升级请求；准入断言同时检查 asked、denied 与 capability_denied |
| 后置工具最终请求一致性 | `backend/tests/test_agent_tool_catalog.py::test_late_middleware_tool_is_visible_everywhere`：OpenAI／DeepSeek 各完成两次 HTTP 离线交换，原 callable 仅执行一次；每个请求直接比较名称、顺序、描述及参数 schema，并对照 WorldState schema_hash 与完整请求预算 |

修复前扩展专项为 7 failed、47 passed（3.54s，`.tmp/tool-catalog-repair-red.txt`）：原两项告警与同类五项旧权限断言。首次增强测试的 list／tuple 比较及同一用例内旧 Shell 审批断言在调试中修正，日志 `.tmp/tool-catalog-repair-focused.txt`；不将这些测试编写中的错误当作应用回归。

| 最新执行范围 | 结果 | 日志 |
| --- | --- | --- |
| 两项告警、authority/access 专项与实际工具工厂 | 72 passed，11.79s | `.tmp/tool-catalog-repair-focused-final.txt` |
| 上文全部 17 个模块，加 test_access_policy 与 test_authority_surfaces，单次联合执行 | 305 passed，103.44s，零失败 | `.tmp/tool-catalog-repair-all.txt` |
| 本 change 12 个 Python 文件的语法、声明式头部与 Harness 依赖方向 | files_checked=12，errors=[] | `.tmp/tool-catalog-repair-static.json` |

305 是单次完整相邻回归计数，不与历史／针对性重跑的数量相加。执行权限、索引实现和旧黄金文件没有修改。

| 维度 | 复核结果 |
| --- | --- |
| Completeness | 18/18 tasks 完成；6/6 requirements 有实现；16/16 scenarios 有正式测试映射 |
| Correctness | 原对象绑定、middleware-first 顺序、真正冲突提前拒绝、原 callable 执行、reload 隔离及权限拒绝均有证据；后置中间件 wire 目录与预算已直接比较 |
| Coherence | 符合 5 项设计决定：来源选择与发现分离、纯目录编译、共享绑定与独立协议转换、插件快照、真实工厂离线验收 |

实现定位（仓库相对路径，行号以当前文件为准）：

| Requirement | 实现证据 |
| --- | --- |
| Plugin tools have a single execution injection owner | `backend/packages/harness/focus/tools/catalog.py:55`；`backend/app/desktop/service.py:2047`；`backend/packages/harness/focus/agents/lead/agent.py:130`、`:168`；完整发现仍为 `backend/packages/harness/focus/tools/tools.py:94` |
| Tool name conflicts fail before graph execution | `backend/packages/harness/focus/tools/catalog.py:67` 的联合校验先于 `backend/packages/harness/focus/agents/lead/agent.py:193` 的 create_agent；错误值对象在 `catalog.py:48` |
| All capability consumers agree on the effective directory | `backend/packages/harness/focus/agents/lead/agent.py:174`、`:180`、`:189`、`:195`；Provider 独立检查仍在 `backend/packages/harness/focus/models/response_projection.py:22` |
| Assembly snapshots preserve tool identity and reload isolation | `backend/packages/harness/focus/tools/catalog.py:19`、`:30`；`backend/packages/harness/focus/plugins/bridge.py:48`、`:52`；reload 通过 `backend/packages/harness/focus/plugins/__init__.py:61` 创建新 registry |
| Role composition preserves valid tool behavior | `backend/app/desktop/service.py:2039` 起保留角色工具组合；`backend/packages/harness/focus/agents/lead/agent.py:168` 所有角色统一插件桥；hook 分发逻辑保持既有行为 |
| Tool visibility does not expand execution authority | `backend/packages/harness/focus/agents/lead/agent.py:171` 保留 AccessPolicyMiddleware 与 ToolExecutionMiddleware；`catalog.py:84` 只包装原工具引用，不重建效果契约或 runtime schema |

### CRITICAL：0

没有发现未实现要求、未完成任务或与设计矛盾的阻断问题。

### WARNING：0

原两项基线告警已校正并加强验收，安全相邻专项中的同类旧断言同步关闭。文件模式边界与敏感配置审批保持现有实现，没有放宽权限。

### SUGGESTION：0

后置 attempt 工具场景已完成两种 Provider 的实际离线调用和最终目录直接对比，增强建议关闭。

结论：本 change 全部核验发现已修复，18 项任务交付，305 项相邻回归通过，可归档。完整全仓测试、真实 API 和 Electron E2E 未运行；没有归档、提交、推送或部署。
