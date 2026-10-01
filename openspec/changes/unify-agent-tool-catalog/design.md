## Context

动机见 proposal.md。当前工具发现入口 get_available_tools 聚合 builtin、custom、MCP 和 plugin，同时供 equipment 展示与 Agent 装配使用。Desktop Main 只排除 builtin；Harness tools=None 使用整个工具池。make_lead_agent 随后统一装配 PluginBridgeMiddleware，其 tools 属性再次返回插件注册表里的工具。

安装版与仓库均为 9be053f。只读诊断确认 demo 插件为 active，目录包含一个 demo_echo；工具池与插件桥返回同一 BaseTool 对象。当前安装配置 compression.enabled=true。实际 Desktop 工厂复现栈为 factory → make_lead_agent:186 → function_specs:35，尚未执行 Provider HTTP、工具副作用或 Commitment。移除工具池的插件路径可保留压缩并成功构图；关闭压缩仅避开提前检查，没有修复所有权。

LangChain create_agent 自动合并 middleware.tools 与普通 tools，工具节点随后以名称构建映射。当前 Focus 的 visible_tools 却直接串接普通工具与中间件工具；WorldState、压缩预算与路由因此存在不同的归并行为。此前桌面测试将外部工具池替换为空，Responses 角色测试传 tools=[]，未覆盖该生产组合。

前次诊断已查询 Context7 的 LangChain 自定义中间件文档，确认 tools 是中间件提供工具的编译期接口。实施前必须重新查询并核对安装依赖的实际合并行为，不能将本段诊断视为未来 apply 的已完成 preflight。

## Goals / Non-Goals

**Goals:**

- 明确每个来源的注入所有者，修复 Desktop Main 和默认 Harness 两个入口。
- 构图前验证联合目录，压缩启停和 Provider 协议不影响冲突处理。
- 所有消费者基于同一组工具对象与稳定顺序；插件效果契约、权限校验和执行 ledger 继续绑定原对象。
- 验收真实组合根和实际工具执行，避免以工具池为空的模型测试代替装配测试。

**Non-Goals:**

- 本 change 不实现热修改运行中图的工具、动态工具搜索、工具别名或自动 namespace。
- 不重写 LangGraph runtime、Provider adapter、插件 hook 协议或插件注册表依赖解析。
- 不增加工具数据库、持久化目录、ContextRevision schema 或新的权限来源；不处理不相关的历史数据与 UI 功能。

## Decisions

### 1. 插件桥拥有插件工具注入；发现目录继续完整展示

get_available_tools 保留“全部可发现工具”的现有语义，equipment 仍能列出插件。执行装配使用按 ToolInfo.source 选择的共享窄入口：Desktop Main 排除已由工作区入口注入的 builtin，并排除 middleware 拥有的 plugin；Harness 默认入口保留 builtin／custom／MCP，排除 plugin。已有显式 tools 参数继续表示调用方提供的普通工具，不按名称猜测来源或自动剥除冲突项。

替代方案：从 get_available_tools 全局删掉 plugin 会导致 equipment 丢失能力；仅在 Desktop 过滤则遗漏 tools=None；移除插件桥的 tools 则破坏其他角色通过 middleware 获得插件能力的路径。采用执行选择与发现展示分离，复用一个来源选择规则，不在两个工厂复制不一致的过滤补丁。

### 2. 小型 Harness 目录编译模块负责验证，不负责工具加载和执行

建议模块 focus/tools/catalog.py，公开 compile_agent_tool_catalog 及只读 AgentToolCatalog 值对象。输入为普通工具、已装配中间件的工具声明和对应 owner 描述；输出保留普通工具传给 create_agent 的入口，以及完整的联合 binding 快照。每个 binding 至少保留原工具对象、名称和装配 owner；来源族在调用方可提供时保留，显式调用方没有来源族时记录入口／位置，不伪造 plugin 或 MCP 来源。

联合顺序采用安装 LangChain 的实际有效顺序，当前为 middleware 工具在前、普通工具在后。返回 tuple 或等价只读结构，不复制／重建 BaseTool，不剥除 metadata、效果契约或 runtime 参数。重复检查必须在任何按名称映射之前完成；即使两项引用同一个对象，也报告重复注入，不能把装配错误视为合法的幂等登记。

所有重复名称拒绝，包括相同 schema、不同 callable、不同权限效果及同一对象重复引用。错误包含冲突名称和两侧 owner，不输出参数值、用户内容或凭据。名称格式与 Provider 兼容性仍归投影模块负责，避免通用 catalog 抄一份 Responses 校验。

替代方案：直接 dict 去重会隐藏真实覆盖；全局新增大型 Registry 会与现有发现、插件和 MCP 注册表争夺职责；将来源存入工具 metadata 会污染既有安全契约。采用纯编译器加小值对象，helper 放受保护函数，有限的方法只提供目录视图。

### 3. 联合目录是能力事实，Provider 保留协议投影

make_lead_agent 在工具提供者中间件装配完成后编译一次 catalog，然后配置 WorldState、CompressionGate，并调用 create_agent。create_agent 仍通过原来的普通 tools 与 middleware.tools 两条合法入口接收工具；两条入口的联合内容必须与已验证 catalog 完全一致。后续新增的 WorldState／typed history／attempt 中间件当前不提供工具，测试约束未来提供工具时也必须纳入编译。

WorldState 的描述和 schema_hash、压缩估算的 function specs、模型 tools 以及实际工具节点以同一组 binding 为输入。共享的是目录与工具绑定，协议 schema 仍由现有转换负责；nested Chat schema 与 flattened Responses schema 不要求原始 JSON 相同，但规范化后的名称、描述、参数 schema、顺序必须一致。已有 Provider 最后一道重复校验保留，核心修复不放在模型 bind_tools 内。

不用增加新的持久化 tools_hash 格式；保留已有 request manifest／Provider source manifest 的哈希语义，并以规范化目录一致性及最终请求预算测试验证。原有权限中间件继续决定每次调用是否能执行，目录存在本身不授予权限。

### 4. 插件目录在构图时快照，reload 只影响后续装配

PluginBridgeMiddleware 捕获本次 registry 的工具列表，返回独立列表或只读视图，防止消费者改动返回列表造成装配漂移；原工具对象保持一致。reload 构建新 registry；已有图继续使用旧 binding，新图按最新 registry 重新编译。插件 hook 的调度与失败策略保持既有行为，避免为本次目录修复扩展 hook 生命周期。

不增加永久冻结整个插件注册表或深复制工具对象的机制。此设计覆盖受支持的 reload 路径；运行时任意修改 BaseTool 的名称／schema 不成为新的支持能力。

### 5. 验证覆盖组合根，使用离线 Provider 与隔离状态

必须通过 DesktopService._build_agent_factory 的 Main 路径，保留 get_available_tools 中的非空插件来源和真实插件桥，不得把两者之一 mock 成空。用隔离 registry 注册有计数器的测试插件工具，Provider 使用 MockTransport；构图时断言零 HTTP，正常运行时发出一次工具调用，核对实际 callable 执行一次及结果闭合。单独覆盖安装内置 demo 插件的图装配，防止 fixture 与发行目录脱节。

覆盖 OpenAI／DeepSeek Responses、压缩启停、tools=None 与显式 tools、其他角色的插件注入，以及合法 builtin／custom／MCP 工具保留。真正同名冲突用不同 callable 和不同 metadata 构造，并证明失败发生在 HTTP 与工具副作用之前；对 middleware 内部重复、普通列表重复、跨入口重复均验收。

WorldState 能力描述、最终 request schema、执行节点工具目录及 CompressionGate 预算与实际 Responses 请求估算对照。测试记录规范化与现有 manifest 哈希的区别，不能用同一个新 helper 的两次调用互相证明正确。

## Risks / Trade-offs

- [旧配置被静默覆盖的工具现在拒绝] → 错误给出名称与双方 owner；不自动改名，用户可修正冲突配置。
- [执行来源过滤遗漏工具或导致 equipment 不显示插件] → 发现入口保持兼容，回归分别验证展示和各角色实际能力。
- [LangChain 合并顺序变化] → Context7 与安装源码 preflight，真实 create_agent／HTTP payload／ToolNode 的独立断言约束顺序。
- [目录一致但安全 metadata 丢失] → 保留原对象，执行已有安全准入测试，并校验效果契约与只读拒绝行为。
- [旧模型测试再次掩盖真实装配] → 独立组合根测试必须同时启用非空插件池、插件桥与压缩，不以空工具 fixture 验收。
- [快照仅冻结列表，不能冻结任意第三方对象内部] → 明确只支持构图配置与 reload；本 change 不增加运行中修改 schema 的能力。

## Migration Plan

1. 用户审核 artifacts 并明确同意 apply；本轮不修改应用代码。
2. 实施前重新调用 Context7，记录 LangChain middleware tools 和 create_agent 合并约定；工具缺失则停止询问用户。
3. 在隔离测试中建立截图错误的失败回归，再引入来源选择与 catalog 编译，接入两个工厂和三个能力消费者。
4. 完成组合根、冲突、reload、权限及目录／预算验证；更新文件头说明和本 change 的测试证据。
5. 部署需重新装配运行图，安装内置 demo 插件且 compression.enabled=true 的 Main 能发送普通消息。部署与推送不由本次 artifacts 创建自动授权。

不迁移数据库或历史 revision。若回退代码则恢复原装配行为及其重复错误；关闭压缩只能作为已知诊断对照，不能标记为修复。

## Verification follow-up

用户在核验后要求全部修复，授权将两项基线测试告警及后置工具 wire 验证建议纳入收尾。当前语义索引合同已为 v8，旧 v5 黄金文件继续以固定哈希和不改写验收。权限验收按现有执行实现组织：只读拒绝所有写入；workspace-write 拒绝根外写入、根内敏感配置进入 ASK；danger-full-access 解除普通文件边界但保留敏感配置审批。测试隔离配置目录并覆盖内外路径及三种模式，不能改宽权限来满足旧断言。相邻 authority tests 中同类的旧默认值、外部读取与敏感写入断言同步校正。后置 attempt 工具使用真实图和 MockTransport 完成调用，直接验证两种 Provider 请求目录与原对象绑定；不增加运行时模块、数据迁移或依赖。
