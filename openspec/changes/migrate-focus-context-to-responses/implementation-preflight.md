# Implementation preflight

用户在 artifacts 完成后显式调用 `openspec-apply-change`，授权实施 `migrate-focus-context-to-responses`。规划完成本身没有被用作授权。

改应用代码之前，已重新发现并实际调用 Context7 resolve 和 query 工具，选择 `/langchain-ai/docs`，核对自定义 middleware state、before／after model 生命周期、checkpoint 持久化和 Responses 接口。官方资料说明 wrap_model_call 的临时消息修改不等于持久化；可恢复更新须经过 graph state／checkpoint，节点重试不能保证外部副作用 exactly-once。

来源：[Custom middleware](https://docs.langchain.com/oss/python/langchain/middleware/custom)、[Context engineering](https://docs.langchain.com/oss/python/langchain/context-engineering)、[ChatOpenAI](https://docs.langchain.com/oss/python/integrations/chat/openai)。Provider wire 差异继续依据 research.md 中官方来源与离线 fixture 核对。

当前依赖基线为 langchain 1.3.10、langchain-core 1.4.7、langchain-openai 1.3.2、langgraph 1.2.6、openai 2.43.0；后续固定经测试的兼容范围，不假定下游升级仍符合相同私有转换合同。

模型调用清单：统一 `create_chat_model` 服务 Main／协作 Agent、gateway 初始化、memory 摘要、Compression、model settings 测试、Curator engine、Round orchestration 和 structured cognitive worker；structured output 出现在后三类认知入口。没有绕过该工厂的应用 ChatOpenAI 实例化入口。每一入口都须验证有效 protocol。

Gate 1 验证 typed codec／V1-V2／semantic 及引用；Gate 2 验证 checkpoint WorldState／分支／投递／权限；Gate 3 验证双 Provider 请求和流式终态及所有角色；Gate 4 清理旧职责、运行回归和记录限制。只有通过当前 gate 才推进协议默认切换。

本次 Context7 成功调用发生在应用源码编辑之前。若后续需要的 Context7 能力不可用，按用户规则停止并询问；不以缓存研究代替缺失文档。

WorldState/inbox 耐久边界再次通过 Context7 核对：LangGraph `durability="sync"` 在下一步前完成 checkpoint 持久化，默认 async 不能充当投递确认前提。统一 Run worker 已使用 sync；模型节点用 `checkpoint_map[""]` 定位其精确 parent checkpoint，inbox receipt 必须验证其中的 typed 来源。
来源：[Durable execution](https://docs.langchain.com/oss/python/langgraph/durable-execution)、[Checkpoint runtime](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/checkpointers.mdx)。

进入 Provider 投影前成功调用 Context7 `/openai/openai-python`，核对官方 Responses create/stream、store=false、encrypted reasoning replay、typed terminal 和 text.format；另复核 DeepSeek 官方 guide/create-response，确认 developer 降为 user、无状态、reasoning_text 与图像 role 限制。此阶段采用独立 Provider 投影/decoder/stream 模块，LangGraph 仍使用 BaseChatModel bridge。

客户端资源调整前再次成功调用 Context7 `/openai/openai-python`，核对同步 close、异步 await close、自定义 HTTP 客户端关闭及 manual replay 合同。当前测试固定 OpenAI 2.43.0/httpx，未因最新文档的 httpx2 示例擅自升级。SDK 实例延迟到首次实际请求构造，同步/异步生命周期独立；配置校验不加载多余 TLS 客户端。
来源：[SDK README](https://github.com/openai/openai-python/blob/main/README.md)、[SDK client lifecycle](https://github.com/openai/openai-python/blob/main/src/openai/_base_client.py)。
