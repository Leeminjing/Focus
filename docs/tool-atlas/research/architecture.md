# Focus 工具目录与覆盖边界

调查日期：2026-11-15。依据目标 main 的源码快照，未读取用户密钥、未连接远端 MCP、未执行用户自定义工具。

发布依据：`5feed0d85284d145759baf7b5373ad80ee8edea3`。工具清单按此提交的源码校验。

## 静态清单如何计数

对 Git 跟踪的 `backend/packages/harness/focus/**/*.py`、`backend/app/**/*.py`、`plugins/**/*.py` 做 Python AST 扫描，提取 `@tool` 与 `StructuredTool.from_function(name="固定名")`，得到 **44 处声明、43 个不同固定名称**。`find_reference_urls` 在原实现和承诺 Worker 追踪包装中各声明一次，按一个工具名计数。43 名中 `delegate_with_review` 是代码驱动的内部 ToolNode，故仓库固定模型工具为 **42 个不同名称**。完整可机器核对的声明来源在 [coverage.json](coverage.json)。这不代表任何一次运行同时装配 42 个。

另外列出 **1 个内部编排工具** `delegate_with_review` 和 **2 个内部远端适配工具** `resolve-library-id`、`query-docs`。后两者是运行时发现的真实 MCP 工具；静态源码只能确认使用的名称与调用字段，不能确认服务未来的 schema 或全部远端工具集合。因此图鉴可共收录 45 项，但必须把「42 个固定模型名 / 1 个内部编排 / 2 个内部远端适配」分开说明。固定插件 `demo_echo` 包含在 42 名内；其他动态插件不在固定计数内。

静态声明覆盖是检索完整性的证据，实际运行可用性还要经过来源发现、角色筛选、环境筛选、中间件注入和联合目录编译。并非所有 Python API、前端按钮、HTTP 路由、巡检协议字段都是模型工具。

## 从来源发现到真正模型目录

1. `get_available_tools()` 顺序聚合 builtin、custom、MCP、plugin，各项是保留原工具对象的 `ToolInfo`；单个来源报错只记录日志，其他来源继续。来源：`backend/packages/harness/focus/tools/tools.py:48`、`:95`。
2. `execution_pool_tools()` 按 `ToolInfo.source` 选择发现池，始终排除 plugin，因为插件由插件桥独占注入；`include_builtin=False` 进一步排除 builtin。原始 BaseTool 兼容条目不会靠名称猜来源。来源：`backend/packages/harness/focus/tools/catalog.py:55`。
3. `make_lead_agent(tools=None)` 使用上述发现池并追加 `describe_skill`；若显式传 tools 则由调用方负责基础工具选择。来源：`backend/packages/harness/focus/agents/lead/agent.py:133`。
4. `make_lead_agent` **总是**附加 `PluginBridgeMiddleware(get_plugin_registry())`，即使调用方传 `middlewares=[]`，插件仍会注入。插件桥构图时保存 tuple 快照，每次 `.tools` 返回新列表；reload 只影响新图。来源：`backend/packages/harness/focus/agents/lead/agent.py:173`、`backend/packages/harness/focus/plugins/bridge.py:45`。
5. `compile_agent_tool_catalog` 合并「中间件工具在前、普通工具在后」的同一目录，保留 callable、metadata 与 owner。任何重名（即使同一对象重复）在名称映射前抛 `ToolNameConflict`，不会静默覆盖。来源：`backend/packages/harness/focus/tools/catalog.py:67`。
6. 同一目录被送给执行图、WorldState、Provider schema、压缩预算和只读 preparation observer，避免「展示能用、执行没有」的多份清单。来源：`backend/packages/harness/focus/agents/lead/agent.py:181`、`:193`、`:200`。

## 角色和环境决定装配范围

下表讨论 Desktop 的 `AgentFactory`，并非所有 Harness 调用者。

| 来源 / 能力 | main | teammate / worker | patrol / standard |
|---|---|---|---|
| 工作区文件与四类 shell | 按 permissions | 按 permissions | 按 permissions |
| `web_search` / `web_fetch` | 有 | 有 | 有 |
| Windows `browser_*` | `os.name == nt` 且 ports 有 browsers 时 | 相同条件 | 相同条件 |
| 用户 custom / 普通 MCP 发现池 | 有 | 无 | 无 |
| 注册插件工具 | 插件桥注入 | 插件桥注入 | 插件桥注入 |
| 协作工具 | 按 main 角色 | 按 teammate / worker 角色 | 不注入协作角色工具 |
| 巡检历史 / 协作历史读取工具 | 有 | 无 | 无 |
| `describe_skill` | 有 | 无 | 无 |
| `spawn_agent` / spawn_teammate / spawn_worker / wake_agent / wait_for_swarm | 非 governed Loop 主执行有；Loop 排除该组 | 无 | 无 |
| `report_must_view_images` | 仅 required 图片非空 | 无 | 无 |
| 默认承诺层 | 按 commitment.enabled 装配 | 显式 middlewares=[] 关闭 | 显式 middlewares=[] 关闭 |

依据：`backend/app/desktop/runs/agent_factory.py:70` 到 `:188`。主代理池在 Windows 原生浏览器存在时还过滤普通扩展池的 `browser_` 前缀，以免重复注入（`:87`）。插件来源由桥另行注入，不在这次前缀筛选内。

工作区工具选择由 `select_workspace_tools` 完成：read → read_file/discover_files；write → write_file/edit_file；host_command → bash/powershell/cmd/sh。来源：`backend/packages/harness/focus/tools/builtins/workspace_tools.py:409`。

临时 `spawn_agent` 的子执行显式只传经继承权限过滤的工作区工具，未追加 web、describe_skill 或递归 spawn。但它仍调用通用 `make_lead_agent`，所以全局插件桥仍生效，且默认承诺中间件仍取决于应用配置。不能把这个子代理描述成绝对「只有工作区工具」。子级能力、工作根、访问模式、权柄面单调不放宽。来源：`backend/packages/harness/focus/tools/builtins/spawn_agent_tool.py:64`、`:70`，`backend/packages/harness/focus/security/context.py:184`、`:251`。

## 承诺流程的特殊工具边界

- `find_reference_urls` 是真正模型可选择调用的工具，但只绑定承诺阶段 4 Worker。原实现 `references.py:57`，包装与选择 `delegation.py:653`、`:674`，绑定 `:735`。
- `delegate_with_review` 是真实 `@tool(args_schema=TaskEnvelope)`，由承诺子图 ToolNode 执行（`workflow.py:317`、`:558`）。它的调用由 `_prepare_supervisor_call` 用 Python 确定性构造 AIMessage.tool_calls（`:499`），不是主代理或 Supervisor 模型的自由调用目录。该子图 Supervisor 实际是控制流节点，不是额外采样模型。
- 阶段 5 的 `resolve-library-id`、`query-docs` 与阶段 6 的 `query-docs` 由 Python 直接 `.ainvoke`；Worker 的语言模型先生成技术/问题信息，再由受控代码访问远端。来源：`delegation.py:347`、`:360`、`:395`、`:412`、`:560`、`:577`。
- 承诺层仅在配置启用且消息触发 `/commit`、尚无合同等条件下启动隔离子图；不能把以上工具假设成普通运行常驻工具。来源：`backend/packages/harness/focus/agents/lead/middlewares.py:45`，`backend/packages/harness/focus/agents/commitment/middleware.py:224`。

## 权限与效果不能混为同一标签

工具可见不代表可以执行，`danger-full-access` 也不会补发缺少的能力权限。主执行图无条件添加 `AccessPolicyMiddleware`，执行前检查受治理身份、能力、文件模式、权柄面和审批；Loop 还复核资源租约。来源：`backend/packages/harness/focus/agents/lead/agent.py:176`、`backend/packages/harness/focus/security/middleware.py:75`、`:189`。

| 效果契约 | 含义 |
|---|---|
| structured_fs | 可提前解析具体读写路径，依次检查 read/write 能力与路径策略 |
| sandboxed_shell | Shell 由受管执行器处理，要求 host_command，使用受治理 sandbox 运行 |
| no_local_effect | 不发生本地文件系统效果；仍可能联网、发协作消息、改变数据库或浏览器状态，绝不等于「没有副作用」 |
| delegated_execution | 由另一受治理执行主体承担权限和实际执行 |
| opaque_local | 无法证明本地目标；保护模式下逐次审批，完整文件权限模式可直接执行 |

契约定义与默认回退：`backend/packages/harness/focus/security/effects.py:57`、`:125`。结构化文件策略：`backend/packages/harness/focus/security/policy.py:71`；可写根包含工作区及共享系统临时区，有显式 allow_global_config 时增加 Focus 全局目录（`:120`）。读取并非一律限制在工作区，受保护权柄面仍需按规则判断。

MCP 和进程外插件不允许用外来自声明放宽权限。摄取时先移除自声明，再按随版本契约、用户显式覆盖、保守 opaque 回退三层签发。Context7 两个固定名称被登记 no_local_effect。来源：`backend/packages/harness/focus/security/trust.py:60`、`:99`、`backend/packages/harness/focus/mcp/tools.py:241`。

注意承诺 Worker 和内部 ToolNode 使用独立 create_agent/ToolNode，而不是上述主图工厂；不能把主图中间件规则自动外推到每个内部适配项。

## 动态来源怎样计数

- **custom**：默认扫描 `~/.focus/tools/<目录>/tool.py`，接受 module.tool 的 BaseTool/RegisterTool，或 build_tool 工厂；目录会执行 Python 入口以装配。实际名称取 `tool.name`，不是必定等于目录名。来源：`backend/packages/harness/focus/tools/registry.py:76`、`:89`、`:117`。本次静态图鉴不扫描用户私有目录，因此不能把 custom=0 当事实。
- **MCP**：按分层扩展配置连接 server、发现工具；单服务器失败跳过，其他继续。内容改变、环境变量有效值改变或连接失败会触发重新发现，正常缓存可复用。来源：`backend/packages/harness/focus/mcp/tools.py:241`、`:325`，`backend/packages/harness/focus/mcp/cache.py:60`。没有运行时快照，无法给出「当前账号全部 MCP 工具总数」。
- **plugin**：固定的 demo_echo 可从仓库源码枚举；进程外插件的名称来自远端 `spec["name"]`，schema 动态转换。来源：`plugins/demo/plugin.py:6`、`backend/packages/harness/focus/plugins/remote.py:225`。插件 API/hook/摄取器本身不是额外模型工具。
- **tool_groups**：`get_available_tools` 保留兼容签名，但其实现不读取 app_config 或 tool_groups；config.yaml 声明式工具机制已移除。不能依据旧分组推断实际工具列表。来源：`backend/packages/harness/focus/tools/tools.py:8`、`:95`。

## 命名与旧文档

`docs/agent-roles.md:26` 把浏览器统称为 focus-dictincet-browser 提供的工具。当前生产工厂直接从 `backend/app/desktop/browser/tools.py` 装配 **11 个 `browser_*`** 原生工具，并使用 `BrowserControlMiddleware`。仓库业务源码未找到 dictincet 命名的实现或导入。旧文档昵称不能作为额外工具计数，图鉴应采用实际 `browser_*` 名称。该文档的 `shell` 同样是四工具统称，不是实际第五个工具名。来源：`backend/app/desktop/runs/agent_factory.py:79`、`:117`，`backend/app/desktop/browser/tools.py:21`。

## 可复现验证建议

1. 静态完整性：用 AST 重建 coverage.json.unique_names；总图鉴除内部动态适配外应覆盖全部43个固定名；同名find_reference_urls两个声明不能误计成两个工具。逐项检查source存在、line指向定义、params格式及内部exposure标记。
2. 构图完整性：使用测试内的隔离 registry/model，不连接用户服务，比较 `compile_agent_tool_catalog(...).tools`、图 `tools_by_name`、Provider function specs、WorldState tool_catalog、CompressionGate schema 的名称与顺序。
3. 角色边界：以 main/teammate/worker/patrol 构图验证custom/MCP仅主代理、插件全角色注入、web唯一、权限筛选、Windows browser条件、governed Loop排除 spawn组。
4. 动态完整性：需要另取实际运行 preparation_observer 工具快照或图工具节点，记录采样时刻与环境；没有该证据就标记动态未枚举，而不是宣称全部远端工具已列完。

已有验证位置（本调查仅阅读，未声称已运行）：

- `backend/tests/test_agent_tool_catalog.py:30`：真实Desktop主图目录、Provider、WorldState及压缩一致；`:82`角色边界；`:136`重名在网络调用前失败；`:182`晚注入工具的一致性。
- `backend/tests/test_tool_catalog_compilation.py:23`：来源选择和框架顺序；`:46`重名及owner；`:62`原始工具兼容来源不按名字猜。
- `backend/tests/test_tool_catalog_snapshot.py:42`：reload新旧快照隔离；`:82`runtime注入不泄露模型schema；`:119`可见写工具仍受只读拒绝。
- `backend/tests/test_tool_effects.py:149`：内置契约完整；`:203`协作契约；`:216`派生与等待；`:227`只读历史工具。
