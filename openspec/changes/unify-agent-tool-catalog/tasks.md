实施门槛：这些 artifacts 完成不等于获得 apply 授权；必须等待用户明确同意。批准后才执行下列任务，并在第一次代码／测试修改前成功调用 Context7。任务只有交付且验证通过后才能勾选。

## 1. 理论预检与失败回归

- [x] 1.1 重新调用 Context7 了解 LangChain middleware.tools 注册、create_agent 的工具合并与 ToolNode 路由；核对安装版本源码，将版本、资料与实际顺序写入本 change 的 implementation-preflight.md；若 Context7 不可用立即停下询问用户，以成功查询和文档记录验收。
- [x] 1.2 在隔离插件 registry 和离线模型下建立实际 Desktop Main 工厂失败回归，保留非空插件来源、真实插件桥及 compression.enabled=true；确认失败精确为重复工具名且 HTTP／工具副作用计数均为零，以修复前可重复的失败输出验收。

## 2. 目录编译与来源选择

- [x] 2.1 在 Harness tools 边界实现小型纯目录编译器及只读 binding 值对象，保存原 BaseTool、名称、owner 和与框架一致的有效顺序；以稳定顺序、原对象身份、metadata 保留和返回容器修改隔离测试验收。
- [x] 2.2 在任何名称映射前统一拒绝普通工具重复、中间件内部重复及跨入口重复，错误包含名称和双方 owner；同一对象和相同 schema 也不静默去重，以冲突测试和零 HTTP／零副作用断言验收。
- [x] 2.3 实现共享的执行工具池来源选择入口，保持 get_available_tools 和 equipment 的完整发现语义；以 builtin／custom／MCP 保留、plugin 由专属路径注入及 equipment 仍显示插件的测试验收。

## 3. 工厂与能力消费者接入

- [x] 3.1 将 Desktop Main 和 Harness tools=None 接入执行来源选择，保留显式 tools 的调用方声明与其他角色行为；以两个真实工厂均只装配一次插件、合法普通工具未丢失的测试验收。
- [x] 3.2 在 make_lead_agent 的统一装配边界编译 catalog，接入 create_agent、WorldState 和 CompressionGate，并保留 Provider 投影终端校验；以所有工具提供者均被纳入、规范化 schema／有效顺序一致及启停压缩都提前拒绝冲突的测试验收。
- [x] 3.3 使插件桥工具列表形成构图快照，保持原 callable、metadata 和 hook 调度；以 reload／禁用只影响新图、旧图保留原目录和返回列表不污染其他消费者的测试验收。

## 4. 实际组合与运行回归

- [x] 4.1 将失败回归扩展为 OpenAI／DeepSeek Responses 与压缩开／关的 Desktop Main 组合，并增加实际内置 demo 插件装配断言；保留非空插件池与真实桥，以 demo_echo 仅出现一次并正常到达离线模型调用验收。
- [x] 4.2 覆盖默认 Harness 工厂、显式工具列表及 Main／Teammate／Worker／Patrol 的角色能力，验证合法普通工具、插件工具和 hooks；以各角色实际图目录及普通工具保留断言验收。
- [x] 4.3 覆盖不同 callable／不同效果 metadata 的真正同名冲突及明确 legacy Chat 协议；以错误指出冲突 owner 且在压缩关闭、模型调用前仍拒绝的测试验收。
- [x] 4.4 用离线 Provider 发起一次实际插件工具调用，独立比较请求 tools、WorldState 能力描述、执行节点绑定与实际 callable 计数，并比较压缩估算与最终请求估算；以两种 Responses Provider 均一次执行、正常输出闭合及目录／预算一致验收。
- [x] 4.5 验证编译后工具仍携带可信效果契约与 runtime 参数，运行只读权限下的写工具拒绝／中断回归；以未产生写副作用且既有安全中间件结果保持一致验收。

## 5. 最终验证与证据

- [x] 5.1 运行工具目录、实际 Desktop 工厂、插件 bridge／reload、Responses、WorldState、压缩与安全的必要相邻专项；检查新增／修改文件头含对外接口、输入输出、工作流与示例，helpers 可见性和 Harness 依赖方向；以实际测试日志、语法检查及 git diff --check 通过验收。
- [x] 5.2 更新本 change 的 verification.md，逐项映射全部 requirements／scenarios 到正式测试，记录失败回归转为通过、实际执行范围和未执行的真实 API／交互 E2E；以 openspec validate unify-agent-tool-catalog --strict 通过、测试引用有效且无未解释发现验收。

## 6. 用户授权的核验发现修复

用户在 verify-change 后明确要求「全部修复」，授权修复两项基线告警、一项请求验证建议，以及相同权限规则导致的相邻旧断言；本节不改变运行时权限或历史数据合同。

- [x] 6.1 修正 legacy v5 索引的当前版本验收，明确验证新 v8 身份，并保留独立黄金哈希、旧版本可读、源文件未改写及新身份可重新加载的证据。
- [x] 6.2 用隔离的真实配置来源覆盖工作区内／外信任配置的读写判定和三档文件模式，修正相邻 authority tests 的旧默认模式、外部读和敏感审批断言；保持执行权限实现不变，以全部安全相邻专项通过验收。
- [x] 6.3 将后置 attempt 工具场景扩展为两种 Responses Provider 的实际离线调用，独立比较请求、路由、WorldState 与预算的名称、顺序和规范化 schema，验证原 callable 一次执行；重跑必要相邻专项、更新核验结论并通过严格校验。
