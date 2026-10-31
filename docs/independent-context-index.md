# 独立 Context 索引接入

`CuratorCoordinationStage` 默认构造 `IndependentPortfolioSemanticIndexService`，模型、存储与预算通过端口接入 `focus-context-index`。原 `PortfolioSemanticIndexService` 保留为冻结对照实现，不会在新路径失败时静默回退。

## 完整发布边界

冻结 Revision 与 semantic view、已提交祖先选择、delegation／Round／fencing 验证继续属于 Focus。独立库负责分组、局部及整体理解、引文验证、共享名额和取消回收。

`FocusIndexStore` 使用既有局部 proof 和完整索引表。局部成功可以立即保存。完整候选只在原 Portfolio 校验 catalog 容量、重验局部赢家和整体 proof 后统一提交；Curator 只接收提交后的 catalog。内部 Engine 的候选返回值不是完整 Portfolio 的耐久发布承诺。

`FocusIndexAdmission` 将每次调用的 `call_id` 绑定到既有预算预留主键。每次尝试验证当前权限与执行 owner，模型期间不持有事务；已报告用量按原账本结算，未知用量维持原 unknown 恢复规则。重试单独准入。批次汇总跳过这些已托管尝试，避免重复计费。角色模型使用 `max_attempts=1`，由独立库掌管响应解析和校验重试。

宿主仅执行一次构建编排，不叠加原服务的整次构建重试。否则配置两次响应尝试会变成四次物理调用，并可能在结算失败后重新调用模型。逐次回执仍记录全部实际尝试。

接入不需要新增数据表或改变已存在 proof 的哈希算法。依赖应固定到独立库的已审阅提交，桌面依赖快照与 harness 声明同步更新。回滚可以恢复 Curator 的原服务导入；已有索引与局部 proof 仍按各自合同隔离读取。

## 验证

使用独立的 PostgreSQL 17 + pgvector 测试实例，设置 `FOCUS_DATABASE_URL` 后运行：

```powershell
python -m pytest backend/tests/test_independent_context_index.py -q
$env:FOCUS_INDEX_COMPARISON_REPEATS = '3'
$env:FOCUS_INDEX_COMPARISON_REPORT_DIR = 'C:\Temp\focus-index-comparison'
python -m pytest backend/tests/test_index_engine_comparison.py -q
```

原有 pytest fixture 会创建并迁移独立随机测试库，结束后删除；不要将 `FOCUS_TEST_DATABASE_URL` 指向正式库。新的接入测试涵盖冻结输入到完整 catalog／Curator 输入、源引用重绑、已用尽预算、缓存命中前重验范围、缺少局部凭据阻断、catalog 拒绝时不发布完整索引、重复取消后的耐久结算及预热复用，另验证响应重试不叠加、结算错误不触发重采样。

比较用例针对 1／4／16 个 Context 共用同一服务，分别记录 cold／append／exact 的外层等待、整批完成、短 Context 等待、队列 P95、模型调用和账本实际增量。新旧实现使用同一批冻结合成 Revisions 与模型配置，开始每个实现前只清理该测试播种 Context 的缓存；实现顺序按 trial seed 打乱。默认单次用作回归，正式测量应使用多次重复。

当前语料为合成数据，模型是带显式 2ms 模拟延迟的脚本端口，报告中 `quality_pass=null`。它验证协议与消费等价并暴露额外开销，不能证明真实模型质量或生产提速。用户暂时没有可用真实历史，真实对照状态为 `pending_data`。

## 2026-10-31 合成对照记录

基线为 Focus `853969375f9df46936552809dce3d543dd2403e4` 的原服务，新服务使用独立库 `af8e65658d9c5351ae5ca0d68ec2ac094f5f240c`。Windows、Python 3.11、PostgreSQL 17 + pgvector，并发上限 4；1 个长 Context（55 → 57 条），其余短 Context（2 → 3 条）。每个 Context 数重复三次，seed 打乱实现顺序，共 9 份完整报告、54 个实现阶段。表中为整批完成时间中位数，单位 ms；调用数为每阶段每轮实际总数，新旧相同，全部与耐久账本增量相符。

| Context 数 | 阶段 | 原服务 | 独立服务 | 模型调用数 |
|---|---|---:|---:|---:|
| 1 | cold | 1635.7 | 2113.8 | 11 |
| 1 | append | 554.7 | 1154.8 | 3 |
| 1 | exact | 49.9 | 138.5 | 0 |
| 4 | cold | 2845.3 | 3443.8 | 20 |
| 4 | append | 1736.7 | 2559.4 | 12 |
| 4 | exact | 84.7 | 201.6 | 0 |
| 16 | cold | 8408.9 | 8654.8 | 56 |
| 16 | append | 8065.9 | 6636.4 | 48 |
| 16 | exact | 1013.3 | 792.0 | 0 |

这些数字包含数据库、适配转换和校验，受本机负载影响。2ms 脚本延迟不能代表真实 Provider 的成本结构；三次重复也不足以排除系统噪声。独立适配在若干场景存在额外耗时，需要继续定位，不能将本表当作生产提速验收。原始 JSON 在本地独立库仓库被忽略的 `benchmark-results/host-comparison-repeated/`；上述测试命令可以重建同结构报告。
