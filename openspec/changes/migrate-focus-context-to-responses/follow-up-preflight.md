# Follow-up implementation preflight

2026-10-01。用户在审阅补充 artifacts 后显式调用 openspec-apply-change，授权实施 tasks 第 12 节；不包含自动提交、推送或归档。

改应用代码前已成功调用 Context7 resolve_library_id 和 query_docs：

- `/websites/langchain_oss_python_langgraph`：add_messages 对新 ID append、同 ID overwrite；节点在 checkpoint 边界恢复时重新执行；精确 checkpoint 读取及 interrupt resume。来源：[Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)、[Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers)。决定：保留用户输入，合同使用独立稳定 ID，交付节点可从已完成子 checkpoint 幂等重试，父 messages 与 Items 同次状态提交。
- `/websites/developers_openai_api`：普通 Responses 手工 replay；Multi-agent Beta 中 encrypted agent_message 与 API-hosted coordination。来源：[Multi-agent Responses](https://developers.openai.com/api/docs/guides/responses-multi-agent)、[Deployment checklist](https://developers.openai.com/api/docs/guides/deployment-checklist)。决定：普通双 Provider user message 投影不变；不新增 Beta 行为。

Context7 为理论依据，不是本机库或真实 Provider 的执行证明；后续用实际依赖、真实 graph／隔离数据库／wire fixtures 验证。旧测试不用于宣称新增范围完成。
