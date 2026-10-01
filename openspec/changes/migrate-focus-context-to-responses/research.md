# Planning research

核对日期：2026-10-01。范围为本地源码、已安装依赖、Context7 和 Provider 官方文档。此文件只记录规划依据，不代表实现 preflight 已完成，也不代表真实 API 已通过测试。

## Context7 evidence

已发现并实际调用 `mcp__context7__resolve_library_id`，选择 `/langchain-ai/docs`；随后调用 `mcp__context7__query_docs` 两次：

1. ChatOpenAI Responses 手工 history 管理和 reasoning output blocks。返回官方 `openai.mdx`、LangChain v1 migration 文档：支持 `use_responses_api=True`，可以客户端追加消息，Responses Items 在新输出格式中进入 message content。该结果不证明 Focus 现有 codec 无损。
2. LangGraph 精确 checkpoint replay／fork 和恢复副作用。返回 `use-time-travel.mdx`、`graph-api.mdx`：从指定 checkpoint 更新产生新 checkpoint，恢复可能重新执行 node；checkpoint 在 super-step 边界保存，函数内部副作用不能被误认为自动 exactly-once。

官方来源：[ChatOpenAI](https://docs.langchain.com/oss/python/integrations/chat/openai)、[Time travel](https://docs.langchain.com/oss/python/langgraph/use-time-travel)、[Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)。

用户要求 apply 前再次调用 Context7；届时必须针对实际使用的库和事务／状态边界重新核对。工具不存在或不可用时立即停止并询问用户。

## Installed dependency baseline

通过本机 Python 的包元数据读取：

| Package | Installed version |
|---|---|
| langchain | 1.3.10 |
| langchain-core | 1.4.7 |
| langchain-openai | 1.3.2 |
| langgraph | 1.2.6 |
| openai | 2.43.0 |

这些只是当前环境版本；apply 时重新采集并经 fixture／合同测试后确定仓库支持范围。本机 `langchain_openai/chat_models/base.py` 的流处理覆盖 reasoning summary 事件，未见 DeepSeek reasoning_text delta 的对应分支；需要窄适配，不能假定启用 Responses 参数即完整支持两个 Provider。

## Provider and Codex sources

- [OpenAI Responses migration](https://developers.openai.com/api/docs/guides/migrate-to-responses)：Items 与 request-level instructions／tools；客户端手工历史管理；structured output 使用 text.format。
- [OpenAI reasoning](https://developers.openai.com/api/docs/guides/reasoning)：continuation 与可见 summary 的区别，工具循环保留相关 Items；配置控制项具有模型条件，不能把 Codex 枚举视为普遍能力。
- [DeepSeek Responses guide](https://api-docs.deepseek.com/guides/responses_api/)：无状态客户端 replay；developer 按 user；hosted tools 与 custom tool 限制；plaintext reasoning 和 typed terminal events。模型能力仍需显式声明及实际请求校验。
- [Codex pinned WorldState](https://github.com/openai/codex/blob/e1522188e908c9fad0acb3edbff8834f06ed5837/codex-rs/core/src/context/world_state/mod.rs)：snapshot 比较区分 absent／unknown／known，并考虑 retained fragment，支持本设计的基线重建规则。
- [Codex contextual user metadata](https://github.com/openai/codex/blob/e1522188e908c9fad0acb3edbff8834f06ed5837/codex-rs/core/src/context/contextual_user_message.rs)：hidden runtime context 不是用户授权；授权区分依赖宿主 metadata。

## Local source findings

| Source | Finding shaping this design |
|---|---|
| `context_evolution/schemas.py`、`models.py` | 精确 checkpoint 身份、不可变 Revision、有序来源和 publication receipt 已存在 |
| `context_evolution/reader.py` | checkpoint 模式 authored 可能返回运行消息，V2 需要新明确合同 |
| `context_evolution/checkpoint_writer.py` | 目前依赖前端消息反序列化写 shadow checkpoint |
| `focus/runtime/runs/events.py` | 序列化裁剪元数据；reasoning 只读取旧字段；任意 text block 可能混入正文 |
| `context_expansion/portfolio_index.py`、`semantic_indexer.py` | 全 execution 输入，工具原子单元、fallback、原始 ordinal 和 v5 cache 需要协同迁移 |
| `context_curator/projector.py` | 按旧 role／synthetic 字段筛选，不能直接承担 V2 semantic policy |
| `focus/security/model_context.py`、`middleware.py` | prompt guidance 和实际工具授权为不同职责；工具边界仍刷新当前安全上下文 |
| `desktop/service.py`、`collab.py` | selected 内容拼 prompt；mailbox 组装时即标已读，需要可恢复投递确认 |

本次没有调用真实模型、执行数据库迁移、运行实现测试或修改应用源码。后续验收必须区分文档依据、离线 fixture、持久化恢复测试和真实 Provider smoke。
