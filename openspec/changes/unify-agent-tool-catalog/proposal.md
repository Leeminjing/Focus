## Why

当前 Main 会把同一插件工具同时从工具池和 PluginBridgeMiddleware 注入。安装版 9be053f 中，启用内置 demo 插件与压缩后，发送“你好”即因 demo_echo 重复而在构图阶段失败。Responses 迁移新增的校验暴露了此前由 LangChain 名称映射掩盖的装配缺陷，需要从工具所有权和目录装配边界修复。

## What Changes

- PluginBridgeMiddleware 成为插件工具进入执行图的唯一系统注入路径；Desktop Main 和 Harness 默认工具池仅选择其负责的来源。
- 在 Harness 增加小型、无副作用的工具目录编译模块，在 create_agent 前验证普通工具与 middleware 工具的联合目录，并保存稳定顺序及装配来源。
- 执行路由、WorldState 工具说明、压缩预算与 Provider 请求基于相同的已验证工具绑定；Provider 继续负责协议投影。
- 真正同名冲突在所有配置下提前拒绝，并报告名称和两侧装配来源；不使用覆盖、自动重命名或按名称静默去重。
- 补充实际 Desktop Main 工厂、默认 Harness 工厂、角色、插件 reload、工具执行与请求一致性的回归验收。
- **BREAKING**：此前被 LangChain 名称映射静默覆盖的工具冲突，包括关闭压缩或 legacy Chat 时的冲突，将成为明确的构图错误；合法工具配置与调用名称保持兼容。

## Capabilities

### New Capabilities

- `agent-tool-catalog`: 工具注入所有权、构图前唯一性检查、装配快照以及执行／上下文／预算／请求的一致性。

### Modified Capabilities

无。当前 openspec/specs 中没有已发布的工具装配能力；本 change 独立定义新能力，不改写已完成的 migrate-focus-context-to-responses change。

## Impact

- Harness：focus/tools 的目录编译与来源选择；focus/agents/lead/agent.py；focus/plugins/bridge.py；必要时更新 WorldState／Compression 的目录消费接口。
- Desktop：service.py 的 Main 工具池选择与实际工厂测试。
- Provider：保留 response_projection.function_specs 的协议校验，避免在 HTTP 层补救装配冲突；不引入新 Provider 能力或依赖。
- 数据：不改 ContextRevision、历史 Item、数据库或旧 checkpoint；新图装配读取当前工具快照。
- 工作约束：本轮只编写 artifacts；用户确认后才 apply。实施前必须重新成功调用 Context7，找不到时立即停下询问。代码按模块单一职责组织，声明式说明写在文件头，并同步更新受影响文件头。
