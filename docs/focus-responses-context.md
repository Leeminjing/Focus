# Focus typed context 与 Responses 执行协议

Focus 的上下文权威仍是不可变 Context Revision 与精确 checkpoint。Responses 是 Provider 执行协议；服务器 conversation 或 previous_response_id 不参与 Focus 状态管理。

```text
Context Revision / FocusItem
  ├─ authored：作者定义，保留原始内容及审批合同
  ├─ semantic：宿主资格筛选，供 RSI / Curator / Context Expansion
  ├─ display：独立 UI codec，不携带 opaque continuation
  └─ execution：任务历史、原生 Items、修复及运行状态
                         ↓
       Run 冻结参考 + checkpoint-bound WorldState
                         ↓
       instructions + input[] + function tools + text.format
                         ↓
              OpenAI / DeepSeek Responses
                         ↓
       完整终态校验 → canonical Items → checkpoint / settlement
```

## 模块与边界

| 模块 | 负责 | 权威边界 |
|---|---|---|
| Harness `focus/history` | envelope、无损 codec、V1 adapter、semantic selection、typed call 关联、bridge | messages 是可验证执行桥，不能独立改写 typed authority |
| Harness `focus/context` | full/diff、baseline 引用、WorldState middleware、冻结输入 | 当前权限由工具安全层重新读取，模型看到的旧消息不能授予权限 |
| Harness `focus/models` | 明确 Provider/protocol、request projector、output/stream decoder、structured output | 完成且参数合法的调用才进入工具节点；unknown 保存后默认拒绝 replay |
| Desktop `context_evolution` | V2 repository/reader、shadow writer、revision publication | V1 不原位迁移；新分支剥离父 runtime、Run 选择与 opaque continuity |
| Desktop `context_assembly` / `inbox` | Run 装备冻结、只读投递准备、耐久确认 | catalog 与 selected skill 正文分开；确认前验证精确 checkpoint |
| Desktop `execution_attempts` | 模型尝试审计、工具执行 ledger | partial audit 不进入可 replay 历史；未知工具效果不能盲目重试 |

`BaseInstructions` 独立进入顶层 instructions。Memory、selected skill、材料读取 policy、空间锚点冻结到 Run input。Observation、Progress、Lineage、Mission/Grant 等仍使用既有冻结领域合同，不能作为可变 WorldState diff。

世界状态当前包含 agent_mode、collaboration_mode、environment、permissions、tool_catalog、skills_catalog、model_context。Snapshot 绑定 thread、namespace、workspace、agent、模型与 Provider 投影版本，并维护模型历史实际保留的 section refs。引用被压缩或版本改变时重新完整渲染。权限更新始终 replacement；一次性工具批准不扩展常驻权限。

## 持久化与恢复

V2 的 JSONB `history_payload` 是唯一持久历史载荷。旧消息列继续承载 V1；V1 的 SQL NULL 与兼容 JSON null 都表示无 V2 载荷。持久化 codec 保存完整 LangChain mirror 与原生 typed output，检查镜像冲突；UI 序列化不会承担恢复职责。

`WorldStateMiddleware.abefore_model` 同一 graph 更新提交消息、typed Items、snapshot、prepared request manifest。运行脊柱使用 `durability="sync"`。模型审计验证精确 prepared checkpoint 后记录尝试；completed result 保存后、graph checkpoint 前崩溃，恢复使用已保存 canonical result。Inbox receipt 验证其中的真实消息后才确认；rollback 到不含消息的旧 checkpoint 可以重新投递。

最终请求准备位于 CompressionGate 之后，压缩恢复也会按 admission 时冻结的版本补齐当前 Run 的 Memory、selected Skill 与材料读取 policy。压缩预算通过相同准备端口预览这些块。宿主 Run admission 和兼容 Registrar 为新输入绑定 direct_user／delegated 来源，以及可用的 Run、Revision、checkpoint、directive 和 Round 引用；真实 HTTP Main 启动经过相同边界。Curator copy/compose 保留来源引用，指定 system role 的策展内容也只能投影成任务上下文，不能升级为平台 policy。

原生 reasoning／compaction 的请求来源保存 `focus-responses-prefix-v1` 证明，绑定实际投影后的完整有序 input 和 instructions。重放校验缺失证明或前缀变化时，最终准备在同一 checkpoint 显式重建 execution branch，核对源 typed 历史 hash，清除旧 runtime 与 opaque continuation，然后重新锚定 WorldState 和当前冻结引用。旧 checkpoint 和已发布 Revision 保留原始载荷供审计；新分支记录重建原因和来源哈希。历史图像转为文字、旧 Run 选择被过滤、请求临时附加图片未进入耐久历史，都可能要求此重建；不能把无损存储误当作任意前缀均可重放。

V1 semantic 从 canonical checkpoint 对象适配；若 settlement 保存的是同身份、同协议内容的 UI 消息，读取时只补回可证明的来源元数据，不写回旧数据。合成工具输出由共享 repair 模块生成 `status=error` 与 exclude 资格；删除并行结果中的一个时，空删除标记延后到交换结束，尚存真实结果继续作为证据。

工具 ledger 在执行前保存稳定 intent；已保存结果可复用，副作用已发生但结果未知时停止自动重试。该机制不宣称外部工具 exactly-once。Run finalizer 继续使用既有租约、fence、publication 和 outbox 事务。

RSI 使用 `revision-semantic-index-v6`，分段及依赖 fingerprint 同步升级。运行控制、reasoning、repair、unknown 不进入任务事实；tool output 只在真实调用关联闭合时成为 evidence，不能独立生成 fallback hypothesis。独立 HEAD v5 golden 验证旧 index identity 仍可读取，新版本不误用旧缓存。

## 模型配置

```yaml
context_run_admission: true
models:
  - name: responses-example
    display_name: Responses example
    use: focus.models.responses:FocusResponsesChatModel
    provider: deepseek             # 或 openai
    protocol: responses
    model: your-model-id
    api_key: $YOUR_PROVIDER_API_KEY
    base_url: https://your-provider.example/v1
    context_window: 131072
    supports_image_input: false
    curation_output_method: json_schema
    default: true
```

能力显式声明，不能按模型名字推断图片支持。未知 adapter 不静默切回 Chat。固定旧 `langchain_openai:ChatOpenAI` 与 `focus.models.deepseek:DeepSeekChatOpenAI` 在未声明 protocol 时仍确定性解释为 legacy Chat；用户偏好中的整条旧配置保持该解释。模型设置 API 与 UI 展示有效 Provider/protocol。

Responses SDK 客户端按同步与异步用途分别延迟到首次请求创建；配置校验、工具绑定和未使用客户端关闭不加载 TLS。使用中的客户端遵守 SDK close 合同，保留原有代理优先与直连策略。

两种 Provider 均手工 replay。OpenAI 使用 store=false，并请求 encrypted reasoning continuation；DeepSeek 省略 store/conversation/previous_response_id，policy 使用 system，reasoning_text 原样保存。Registry、MCP 和 plugin 工具继续由 Focus 作为 function tools 执行。Structured Outputs 使用 text.format；schema 与领域 publication 校验保持独立。

DeepSeek 真实验证发现 high reasoning 与强制工具选择组合返回 400；显式 high/xhigh 搭配 required/指定 function 在发送前拒绝。reasoning 工具回合使用 auto 已通过真实 replay；需要强制选择时显式关闭 reasoning 并按 Provider 能力验证。

实际 SDK 请求和压缩门共用完整投影预算，计入基础行为、待追加 WorldState、工具 schema、输出格式及本轮图片。Provider 未报告的 attempt usage 保留 unknown；既有聚合预算仍通过 `last_usage_reported` 区分估算/缺失。终态重复不能重复正文或计量。

## 维护与回退

1. 设置 `context_run_admission: false`，经原配置生效/重启流程暂停新 Run。Desktop admission 与统一执行脊柱均检查该开关；历史 reader 继续读取 V1/V2。
2. 保留支持 V2 的应用版本和当前 schema。数据库迁移拒绝删除已存在的 V2、delivery receipt 或 attempt ledger。
3. 如需 legacy Chat，明确选择固定 legacy adapter 与 `protocol: chat_completions`，通过现有派生/定义/压缩边界创建新 execution branch。新分支保留任务来源，清除 opaque/native continuity 与旧 WorldState 基线。
4. 不把旧 binary 部署到活动 V2 writer 上，不将 Responses reasonings 强塞入 Chat，不原位批量改写 revision 或删除 V2 数据。

恢复继续使用旧 checkpoint 时先验证 retained refs，刷新当前 SecurityContext；旧 snapshot 不能恢复已失效授权。

## 验证入口

离线协议：`pytest backend/tests/test_responses_protocol.py backend/tests/test_responses_role_cutover.py`。

耐久集成：`pytest backend/tests/test_checkpoint_world_state.py backend/tests/test_scoped_context_inbox.py backend/tests/test_execution_attempt_audit.py backend/tests/test_focus_history_migration.py`。仅使用 fixture 创建的隔离 PostgreSQL；DDL/回填测试采用独立临时库。

独立审计修复：`pytest backend/tests/test_request_context_repairs.py backend/tests/test_history_source_repairs.py`。包括真实 Lead 工厂压缩恢复、双 Provider 策展投影、数据库 Run admission、V1 canonical 读取与并行工具 error repair；前缀变化／不变续跑同时使用内存和 PostgreSQL checkpointer，核对旧 checkpoint 不变。

受控 smoke：`python backend/scripts/responses_smoke.py --model <明确目录模型> --report <输出路径>`。工具、手工 replay、JSON schema、reasoning stream 与 replay 分别报告。API key 不进入报告。OpenAI 的 live 验证仍需独立可用配置；离线合同通过不能代替真实服务验证。
