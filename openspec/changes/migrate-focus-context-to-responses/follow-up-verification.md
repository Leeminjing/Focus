# Confirmed contract handoff verification

> 后续独立验证发现 2 WARNING 与 1 SUGGESTION，见 follow-up-independent-verification.md。下列实现与测试记录保留为 apply 时的证据，不能证明被新复现覆盖的边界已经正确。

2026-10-01。用户审阅新增 artifacts 后调用 openspec-apply-change，实施 tasks 第 12 节；Context7 已在修改应用代码前成功调用，依据见 follow-up-preflight.md。没有提交、推送或归档。

## Implementation

- Commitment 的 handoff.py 负责纯编译：第 7 阶段批准与 artifact／合同 hash 校验，delegated／revision 来源，独立稳定 ID 和精确子 checkpoint 引用。workflow.py 保存批准证明与第 6 阶段冻结知识；middleware.py 从完成 checkpoint 恢复交付，保留原输入，并经现有 bridge 同步提交父 messages／execution_items。
- knowledge 作为 selected_context／reference_only 独立保存；父图恢复不读取当前文件。task_contract 为 canonical Item 的兼容视图，shadow 分支从合同恢复缺失镜像，非空冲突拒绝。旧普通消息和旧 Revision 不按标签重分类。
- authored continuation 使用独立消息 ID 接纳合同及冻结引用；既有 publication／fencing 负责结算，没有新增合同数据库或第二写入权威。
- semantic index v7 的 fallback 仅从 index 正文和引用生成假设，混合段的 reference_only／evidence_only 仍可解释／grounding。旧 v6 identity 与验证算法保持只读兼容，新版本通过既有 fingerprint 隔离缓存。
- history/display.py 统一编译合同替换关系与理论依据合并展示；Run events 和 V2 Reader 使用该只读投影。原输入和独立 Item 身份在 canonical history 保持不变，内部知识不重复展示。
- 当前 AgentCollaboration 在两种 Provider 继续使用普通 user message；新增 wire 合同及回归，不启用 Beta、不改变 Inbox ack 或工具授权。request_manifest.source_bindings 记录实际可见 Items 的来源，合同、协作及参考均可回溯。

## Actual validation

Python 3.12；langchain 1.3.10、langchain-core 1.4.7、langgraph 1.2.6、langchain-openai 1.3.2、openai 2.43.0。测试使用显式 PYTHONPATH 包含 repo 与 backend/packages/harness。

| Batch | Actual result | Log |
|---|---|---|
| 后端合同／真实 Lead／承诺批准恢复／PostgreSQL／publication／旧流程／事件／Reader／Inbox／Responses／history／安全专项 | 181 passed，9.48s | .tmp/commitment-handoff-regression.txt |
| interpretation／evidence corpus／缓存兼容／增量索引／派生／Reader／Curator 专项 | 133 passed，78.18s | .tmp/commitment-semantic-regression.txt |
| 桌面 conversation events／reconciler／live consistency／render budget／frame／DOM／run-ui／compression／curator | 72 passed，323ms | .tmp/commitment-display-regression.txt |
| 最终方法拆分及缺失来源诊断后的合同／真实 graph／publication／旧流程／interpretation／typed history 专项 | 122 passed，32.70s | .tmp/commitment-handoff-final.txt |

各批次有重叠，不能相加宣称完整套件结果。新增测试入口：test_commitment_handoff.py、test_commitment_handoff_recovery.py、test_commitment_handoff_publication.py；新增场景具名映射见 scenario-coverage.json。

实际 graph 验证四个既有人工批准节点、知识文件删除后的冻结消费、父 checkpoint 提交前／后故障、完成子图重新构图交付及 PostgreSQL 提交后首次 sampling 恢复。双 Provider 用 MockTransport 读取实际 SDK JSON/SSE 请求和 source manifest；只发布 MAIN_RESULT，承诺子图正文不进入主 tokens。独立数据库 publication 测试通过真实 saver、RunRegistrar／Finalizer、Repository／Reader 与 shadow writer，检查幂等结算、authored／semantic／display、派生镜像恢复、rollback pointer 及旧精确 checkpoint 不变。

模块、声明式文件头、文件内非必要注释及 Harness 依赖方向已检查。新增 fixture 和生产文件都按职责分离；没有为已成立的 Provider fallback 再加运行时开关。OpenSpec strict、场景／测试引用清单和 git diff --check 的最终结果见当前 verify-report.json。

## Limits

本次没有真实 OpenAI／DeepSeek 网络 smoke、交互 Electron E2E 或全量 backend 测试。真实批准流程和真实数据库 publication 分别通过实际组件集成验证，publication fixture 使用独立冻结 handoff，而非用户生产进程从批准到结算实验。权限测试证明合同批准不改变安全上下文，并回归当前安全套件；不宣称穷尽所有具体工具。旧 621／70／180 通过仍仅是原范围证据。
