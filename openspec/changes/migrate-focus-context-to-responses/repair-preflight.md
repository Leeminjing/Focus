# Verification repair preflight

2026-10-01，用户“全部修复”授权修复独立验证报告 F1–F6；原 change 的领域合同和迁移范围继续适用。

改应用代码前已发现并成功调用 Context7 resolve-library-id 与 query-docs：

- `/websites/langchain_oss_python_langgraph`：[interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)、[time travel](https://docs.langchain.com/oss/python/langgraph/use-time-travel)、[graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)。恢复重执行被中断节点，已提交的前序节点不会重新执行；请求准备必须在历史重建之后完成，并进入模型实际消费的同步 checkpoint。
- `/websites/developers_openai_api`：[deployment checklist](https://developers.openai.com/api/docs/guides/deployment-checklist)、[text generation](https://developers.openai.com/api/docs/guides/text-generation)、[programmatic tool calling](https://developers.openai.com/api/docs/guides/tools-programmatic-tool-calling)。无状态 continuation 必须保存完整原生输出并按顺序回传，指令层级由角色表达。Focus 另外要求宿主来源决定角色、连续前缀必须有可验证证明。

实现方向：统一最终请求准备；独立 Provider 前缀证明与 checkpoint 分支重建；宿主 admission 生产来源；canonical V1 读取；共享非成功 repair 构造。保留既有旧历史，不改写已发布 Revision。文档核对不等同于真实 Provider smoke。
