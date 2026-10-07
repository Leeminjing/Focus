# 声明支持校验与作者修订

2026-10-07 的真实 Loop 在桌面宠物 UI 材料综合时，将 outcome 的“模型层”改写为“Model Adapter”，却没有引用明确包含该术语的 required_invariants。独立核验判为 unsupported，原实现随即交回用户，作者无法根据这个判定修正候选。

## 责任边界

StructuredContextSynthesisService 将结构准入、独立 direct-support 核验和确定性 dossier 校验一起接入作者的 invoke_validated。RoleBoundStructuredModel 继续独占尝试次数、请求预算准入、取消和用量记录；没有新增外层重试循环或持久恢复调度器。默认仍为两次作者调用，结构错误和支持拒绝共享此上限。

独立核验产生 supported / unsupported / unknown，作者只能修改新候选的表述或冻结目录中的引用，不能修改原判定。Verifier 判断语义蕴含而非逐字匹配，但不使用未引用的证据或常识补足缺失概念。成功后仍进入原有独立 Context quality gate。

单次 synthesis 调用按声明正文和冻结引用身份复用核验结果。更改 section、claim_key 或覆盖标签不会重新抽取相同声明的 verdict；更改声明或引用后，必须重新独立核验。此复用仅在同一冻结调用内有效，没有跨目标、来源或模型配置的缓存。

核验依赖自身的失败由其原有有界调用处理；耗尽后不会再要求作者重写来解决网络、Schema 或基础设施问题。取消会立即传播，两侧实际消费均由已有入口记录。

## 私有反馈与持久审计

StructuredResultValidationError 的 retry_context 只进入下一次请求的 previous_attempt_correction，参与同一请求预算检查，不进入公开 validation_feedback、blocker 摘要或 attempt 日志。

ContextSynthesisResult.rejected_reviews 保存各个不同的已结构准入拒绝候选及真实独立判定。compiler 将其单独保存为 dossier_synthesis_reviews / recorded；有效 dossier 仍保存为 dossier_synthesis / ready。审计记录不作为 ready 缓存恢复。已有质量修订路径也在其私有结果中保留这些拒绝记录。

服务版本更新为 structured-context-synthesizer-v12，旧版本阶段产物不冒充新合同结果；没有数据库迁移或新依赖。

## 验证

- 真实失败模式：outcome 引用被拒绝，作者修正为 required_invariants，新的声明通过独立核验，原 unsupported 判定仍在审计中。
- 持续 unsupported / unknown：用完作者原有上限，保留阻断，不重复核验相同声明。
- Verifier 超时及取消：不会额外消耗作者修订次数。
- 私有反馈在第二次模型调用前接受预算准入；预算不足时不会发送下一次请求。
- PostgreSQL：有效材料与原拒绝独立持久化；新 compiler 恢复成功材料时不调用模型、不重复消费，拒绝审计不能作为 ready 输入。
- 已有结构纠错、冻结引用、完整 question/requirement 覆盖、开始条件、质量修订、模块边界和用量回归保持通过。

运行测试需将 PYTHONPATH 指向 backend/packages/harness：

```powershell
python -m pytest backend/tests/test_synthesis_support_repair.py -q -p no:cacheprovider
```

本地相关主组合通过 203 项；用量与恢复组合通过 26 项；新增测试最终通过 7 项；最终 synthesis 组合通过 105 项（各组合有重叠，不相加作为独立用例总数）。安装目录的五个产品文件已逐一备份并部署，SHA-256 与仓库源码一致，安装导入确认 v12，Focus Gateway 健康检查正常。原 Loop 114ef645e2bc4e6a9c8a483b7d314133 已通过 UI 正式“重试”进入第 2 轮，16:15:57 启动 Context Run 3e5701a9ef3c4b8f9968ffbd43bbbead。本轮选择直接执行，尚未在真实模型中触发新的 dossier 支持修订路径；生产入口的该路径由受控 Provider 回归测试验证。后台监测会在本聊天跟踪终态或新异常；这些测试不代表 Obsidian 插件已完成。
