# 插件与自定义工具调查边界

调查日期：2026-11-15。调查依据为当前工作区源码，未启动插件、未调用工具、未读取用户私有配置、未查询真实运行中工具清单。

## 可以静态枚举的模型工具

- **1 个：`demo_echo`**。定义：`plugins/demo/plugin.py:7`；注入声明：同文件 `build_plugin`，第 20 行；清单：`plugins/demo/plugin.json`（enabled=true，provides 包含 tool）。完整条目见 `plugins.json`。
- `plugins/` 下当前有 `demo`、`spatial-patrol` 两个含 plugin.json 的插件目录。
- `skills/public/focus-skill-to-strategy/SKILL.md` 是随版本提供的执行指导文档，没有定义、注册或导出模型工具；不能把 Skill 内提及的 HTTP/API 或命令当成独立 tool。

## spatial-patrol / ONLYOFFICE 不是 Agent 工具

- `plugins/spatial-patrol/entry.py:1` 的模块说明明确为“file viewer and manual DOCX editor without Agent tools”。`build_plugin` 在第 10 行，返回空 `PluginDeclaration()`。
- `plugins/spatial-patrol/plugin.json` 的 `provides` 为空数组；它声明的是 HTTP 路由和前端静态资源。
- `plugins/spatial-patrol/docx_routes.py:45` 起的 runtime、session 创建/读取、force-save、close 路由属于桌面编辑功能。`desktop/onlyoffice-focus-bridge/code.js` 负责保存/文档变更桥接，未向模型注册工具。
- `plugins/spatial-patrol/onlyoffice/README.md` 说明 ONLYOFFICE 为手动 DOCX 编辑会话惰性启动的可选运行环境。Docker Compose、HTTP route、JavaScript command、桥接函数均不计入 Agent tool 数量。

## 插件如何进入实际 Agent 工具目录

1. `backend/packages/harness/focus/plugins/__init__.py:34` 的 `_plugin_roots` 默认合并仓库插件根和 `global_home()/plugins`（通常 `~/.focus/plugins`）；显式 root 则只加载该目录。仓库根优先，同名用户插件被拒绝。
2. `plugins/loader.py:118` 的 `load_plugins` 按目录名排序发现 plugin.json，跳过 disabled，校验清单/资源，执行入口 `build_plugin(context)`；或者按 runtime 配置启动进程外插件。构建错误、缺失资源、声明不符、依赖缺失都会阻止对应插件成为 active。
3. `plugins/registry.py:184` 起的 `resolve_dependencies` 解析服务依赖；`_commit` 第 216 行将工具放入注册表，`tools()` 返回 active 插件工具。
4. `plugins/bridge.py:44` 的 `PluginBridgeMiddleware` 捕获工具快照，`tools` 第 51 行返回列表拷贝。`focus/tools/catalog.py:55` 的 `execution_pool_tools` 排除 source=plugin，避免发现池和桥重复注入；第 67 行编译工具目录并拒绝名称冲突。
5. `reload_plugins` 重建注册表，只影响下一次图装配；路由/静态资源挂载在 Gateway 启动时完成，变更需要重启后端。工具可发现、图内已绑定、调用获准是不同条件。

以上第 2–4 项中以 `plugins/` 或 `focus/` 开头的 Python 模块路径，均相对 `backend/packages/harness/focus/`。

## 自定义工具为运行时扩展，不可仅凭仓库穷举

- `backend/packages/harness/focus/tools/registry.py:54` 的 ToolRegistry 默认扫描 `global_home()/tools` 下每个子目录，仅识别 `tool.py` 入口，不扫描当前仓库 skills 目录。
- 提取顺序（同文件 `_resolve_tool` 第 117 行）：`module.tool` 为 BaseTool；`module.tool` 符合 RegisterTool 并经 build_tool 构建；然后尝试模块级 build_tool 工厂。
- ToolInfo.source 为 custom；name 优先取工具本身 name，缺省才是目录名。模块加载/工厂失败则记录日志并跳过。registry 按 root 缓存，reload_tool_registry 影响后续 Run，当前 Run 保持装配快照。
- 本次没有读取 `~/.focus/tools` 或 `~/.focus/plugins` 的私有文件，**运行时自定义工具数量未知**。未知不代表零，也不纳入静态工具总数。

## 进程外插件也是动态工具来源

- `backend/packages/harness/focus/plugins/remote.py:300` 的 build_remote_declaration 发出 hello，以子进程上报的 `tools[]`（name/description/schema）构造工具；远端列表不是源码中的固定工具列表。
- `remote.py:225` 的 build_remote_tool 包装 StructuredTool，调用通过 stdio JSON Lines 发送 tool_call；工具默认超时 120 秒（握手 30 秒），失败转为 ToolException。返回 content 为字符串，非字符串则 str(content)。
- `_args_model` 第 209 行仅进行一层 JSON Schema 类型映射（string/integer/number/boolean/array/object）；模型参数来自握手 schema。不能用包装函数名代替真实远端工具名，也不能杜撰未知远端工具。
- 仓库两个插件清单均没有 runtime，故本次仓库静态列表没有额外的进程外工具。

## 效果契约注意点

`demo_echo` 没有 `declare_effect`，所以尽管实现无实际副作用，`focus/security/effects.py:125` 的 effect_of 仍回退 opaque_local。`security/middleware.py:347` 的 `_admit_opaque` 在 read-only 和 workspace-write 下整体待决，danger-full-access 下直接执行。图鉴应同时说明实现效果和执行准入，不能根据回声行为自行宣称已获 NO_LOCAL_EFFECT 契约。