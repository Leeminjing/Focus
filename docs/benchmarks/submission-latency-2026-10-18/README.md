# 提交到可执行：全 Run 口径

本轮使用 observer v3，四类场景各 3 次。按用户确认，首个 Run／有效输出覆盖本次提交关联的所有 Run，包括当前 Context 的继续运行。派生 Context 是否可用单独测量；已有 Context 开始运行不表示新 Context 已准备好。

## 本轮结果

12 个样本均已落盘，10 次进入 `waiting_user`，2 次超时；0/12 到达全部五项里程碑，0/12 观察到新派生 Context 可用。完整派生链路尚未验证通过，不能据此给出成功派生的端到端分位数或首发性能达标结论。

精确缓存场景有 2/3 次观察到关联 Run 输出，分别约 145 秒和 201 秒；少量追加有 1/3 次观察到输出，约 214 秒。冷构建和长工具输出没有观察到决策提交或 Run 启动。完整分位数、观测数和阶段阻断见 [results.md](results.md)，代码验证及未通过的回归见 [validation.json](validation.json)。

本批合成任务出现独立性拒绝和工作协调失败等阻断。它们不等同于纯粹的性能超时，也不能证明生产派生必然失败。后续应先取得合同允许的成功派生正例，再扩大样本；本轮不据此调整分段参数。每类 3 次不足以解释 P95。

## 指标

所有累计耗时从 HTTP 提交前的同一个单调时钟开始，读取独立事务中已提交的事实：

| 指标 | 证据 |
| --- | --- |
| 耐久受理 | HTTP 返回耐久回执 |
| Patrol 决策提交 | 决策已提交，冻结 Observation 的因果链或 Mission 来源包含该输入 |
| 派生 Context 可用 | create/merge 动作已应用，精确 Revision 有发布凭证、可运行 checkpoint，且投影有效 |
| 首个 Run 开始 | 本次提交相关决策的 Directive 对应 Run 已有 `context.run.started` 事件 |
| 首个有效输出 | 同一关联 Run 已有 `context.output.available` 事件 |

输入被处置后可通过冻结 Mission 的 `input_sources` 继续追踪。创建 Context、启动 Run 可以属于不同决策；Run 保留启动决策的完整身份，另存可选的 Context 创建来源。首个启动与首个输出分别按规范事件 sequence 选择，较晚启动的 Run 可以先产出内容。

有效输出指本次指令之后的非空助手正文或具名工具调用，是可见输出信号，不是内容正确性评分。空响应、隐藏推理与旧历史不计入；事件只保存身份、类型及固定摘要。

## 场景与预算

- cold：新工作区、新 Context，未预建索引。
- exact_cache：提交前用同一生产服务完成完整索引，记录测量阶段出现的 `exact_hit`。
- small_append：准备已发布祖先的完整索引，再发布模拟 Run 结算的少量追加后继，记录测量阶段出现的 `incremental`。
- long_tool_output：冷历史含完整工具交换及 1,500 行工具输出。

使用真实默认模型 `deepseek-flash` 与 normal 总预算，各样本有独立迁移库、临时工作区及真实 checkpoint。没有模型响应替身，生产共享名额、合并构建、缓存、后台预热与分段参数保持原样。

导出的索引阶段诊断没有把模式绑定到目标 `source_revision_id`。模式表只说明样本的测量阶段出现过哪些模式，不能单独证明预置历史走了目标缓存路径；`expected_mode_observed_any_revision` 也仅表示样本级观测。

本机整理输出上限覆写为 65,536，超过 normal 总输出预算 50,000；沿用已有 normal 索引报告口径，仅在测试内存恢复产品 schema 默认值 8,192。模型、角色上限及预算记录于 metadata，没有修改配置文件。这不是对本机全部覆写保持原样的测量。准备阶段不计入提交后的时间，但用量仍消耗同一 normal 预算，报告分别列出准备前后记账。

`normal` 表示配置档位，不代表已验证记账硬上界：`cold-1`、`small_append-0`、`long_tool_output-2` 的已记账总输出分别为 52,925、58,654、50,735 token，超过标称 50,000；原始用量保留于报告，本轮未修改预算执行策略。

## 解读限制

这是进程内 ASGI HTTP 提交到数据库确认的观测上界，包含监督器启动、恢复及轮询开销，不能当作桌面点击到渲染的 SLO。v3 不与本任务的全量回归并行启动，主机、网络和服务端负载仍未受控。

计时对象是已经准备好历史的工作区上的下一次用户提交；最初的工作区登记、Patrol bootstrap 与历史播种在计时前完成，不测首次工作区初始化耗时。“冷构建”指该历史的索引尚未预建。

每次观察窗口为 300 秒。等待、错误、超时及缺失指标均保留；缺失不是 0 毫秒。阶段耗时用于定位，不可相加替代墙钟时间；提交前的准备阶段独立标注，某些同源阶段也会有重复持久投影。

统计为已观测里程碑的 nearest-rank 分位数，同时给出观测数、缺失数与完成率。每类 3 次不足以解释 P95。超出截止窗口后才看到的里程碑保留为审计值，排除出分位数；长时间中断标记 `observer_interrupted`。

样本到达全部五个里程碑才算完整链路完成。有首个输出但未派生新 Context 的样本仍保留已有输出耗时，不冒充派生就绪。

## 复现

```powershell
$env:PYTHONPATH = 'backend/packages/harness;backend/tests;.'
$env:FOCUS_SUBMISSION_REAL_MODEL = '1'
$env:FOCUS_SUBMISSION_BENCHMARK_REPEATS = '3'
$env:FOCUS_SUBMISSION_BENCHMARK_TIMEOUT = '300'
$env:FOCUS_SUBMISSION_BENCHMARK_OUTPUT = '.tmp/submission-latency-observer-v3'
python -m pytest backend/tests/test_submission_latency_real_model.py -q --tb=short
python -m backend.scripts.submission_latency_report .tmp/submission-latency-observer-v3
```

模型验收遇到不完整链路会失败，仍保存原始样本。测量在清理前先落盘，清理异常另外记录。

旧的 observer v2 派生 Run 基线位于 `../submission-latency-observer-v2-2026-10-17/`；前置诊断在 `../submission-latency-preflight-2026-10-17/`。它们不与本批混合统计。
