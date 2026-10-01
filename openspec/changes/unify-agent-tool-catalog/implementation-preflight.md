# Implementation preflight

2026-10-01（本地实施记录）。用户显式调用 openspec-apply-change，授权实施已审核的 unify-agent-tool-catalog；不包含提交、推送、部署或归档。

应用代码及测试修改前，成功调用 Context7 resolve_library_id 与 query_docs，library 为 /websites/langchain_oss_python_langchain。依据：[Class-based middleware](https://docs.langchain.com/oss/python/langchain/middleware/custom)、[Skill middleware](https://docs.langchain.com/oss/python/langchain/multi-agent/skills-sql-assistant)。tools 是 AgentMiddleware 的工具注册声明，create_agent 自动接纳该声明。

本地 Python 3.12 环境：langchain 1.3.10、langchain-core 1.4.7、langgraph 1.2.6。只读 inspect 核对 create_agent 的 middleware_tools + regular_tools 合并顺序，ToolNode.__init__ 的 self._tools_by_name[tool_.name] = tool_ 赋值会覆盖重名。Focus 必须先校验联合声明，而不是依赖该映射；WorldState、预算及模型目录要遵循相同有效顺序。

Context7 返回新版文档示例不作为固定版本源码替代；实施使用上述已安装依赖的真实工厂进行离线测试。不新增依赖或工具执行权限。

2026-10-01 核验发现修复：用户明确要求「全部修复」。再次成功调用 Context7 resolve_library_id 与 query_docs（/websites/langchain_oss_python_langchain），查询 middleware tools 与真实 agent 调用测试；依据 [Agent quickstart](https://docs.langchain.com/oss/python/langchain/quickstart)、[Middleware](https://docs.langchain.com/oss/python/langchain/middleware)。随后只读核对本地 RevisionSemanticIndexer v8、decide_path_access 文件边界先于敏感审批及现有 AccessPolicy 专项。查询发生在本轮任何测试代码修改之前。修复前扩展专项为 7 failed、47 passed，包含原两项告警及五项同类旧权限断言，日志 .tmp/tool-catalog-repair-red.txt。
