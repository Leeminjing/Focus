# Follow-up repair preflight

2026-10-01。用户在独立验证报告后要求“全部修复”，授权 V1／V2／V3 的实现、必要回归及 artifacts 更新。改应用代码前成功调用 Context7 resolve-library-id 和 query-docs；未使用旧 preflight 代替。

- LangGraph：`/websites/langchain_oss_python_langgraph`，查询 state reducer、消息删除、update_state／checkpoint。依据：[graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)、[checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers)、[backward compatibility](https://docs.langchain.com/oss/python/langgraph/backward-compatibility)。多通道更新经过各自 reducer，update_state 创建新 checkpoint，不修改旧 checkpoint；清空 merging channel 不能仅返回空列表；新增兼容 state 字段使用 NotRequired。
- Pydantic：`/pydantic/pydantic`，查询 nested frozen model 与 model_validator。依据：[validators](https://github.com/pydantic/pydantic/blob/main/docs/concepts/validators.md)、[models](https://github.com/pydantic/pydantic/blob/main/docs/concepts/models.md)。跨字段领域规则在 model_validator 校验；model_copy(update=...) 不自动验证，因此不能把展示副本当权威写入；旧不可变 proof 按原版本读取，新验证合同用明确版本与 fingerprint 隔离。

实现约束：不更改 Provider wire、不新增批准节点。共享 semantic grounding 校验来源资格；升级新索引／局部和综合验证合同，旧 payload 身份只读兼容。合同镜像和 typed／legacy 来源标记随历史重建提交，正常镜像冲突仍拒绝；显示附件只读复制自原触发消息。真实 API smoke 不属于此次修复验收。
