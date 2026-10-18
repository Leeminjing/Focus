# 提交到可执行：真实模型全链路 smoke

这是按旧口径试跑的部分基线，未补齐每类 3 次，不是最终验收结果。它只追踪新派生 Context 的 Run；用户随后确认要覆盖本次提交关联的所有 Run，包括当前 Context 的继续运行。最终全 Run 口径位于 `../submission-latency-2026-10-18/`，不与本目录混合统计。

本批原始样本由 observer v2 采集，跨 2026-10-17／18 完成。它可确认待处理／延后输入的因果链，不会回溯恢复输入被处置后仅通过 Mission 来源关联的后继轮次时间。更新后的 v3 探针已增加该来源路径，并经 PostgreSQL 回归验证；本批 v2 真实模型数据不验证这项新增路径，也不与 v3 数据混合统计。

## 指标边界

所有累计时间均从调用 HTTP 提交之前的同一个单调时钟开始。轮询使用独立数据库事务，只确认已经提交的事实。

| 指标 | 确认条件 |
| --- | --- |
| 耐久受理 | HTTP 返回耐久回执 |
| Patrol 决策提交 | 决策的 Observation 因果链或冻结 Mission 来源包含该输入，且 `authority_committed_at` 已提交可见 |
| 派生 Context 可用 | 相关决策的 create/merge 动作已应用，精确 Revision 有发布凭证、可运行 checkpoint，且投影有效 |
| 首个 Run 开始 | 该派生 Revision 的 Run 有执行启动成功之后写入的 `context.run.started` 事件 |
| 首个有效输出 | 同一 Run 在本次指令消息之后产生非空助手正文或具名工具调用，并耐久记录 `context.output.available` |

首个决策与后续派生、启动可以属于不同轮次；关联必须来自同一输入的因果链。候选 Context、局部段缓存、完整索引以及仅调度为 running 的 Run 均不足以证明后续里程碑已经到达。

输入被处置后可能从待处理因果链移除，后继轮次仍可通过冻结 Mission 的精确 `input_sources` 关联。仅仅属于同一 Loop 不足以认领本次提交的成果。

“有效输出”是有可见正文或工具调用的首个执行结果信号，不是内容正确性或任务完成的评分；隐藏推理、空响应和旧历史不会计入。输出事件只保存身份与类型，不保存正文。

## 四种场景

- cold：新工作区、新 Context 和未预构建的历史。
- exact_cache：计时前，用同一生产服务完成目标 Revision 的完整索引构建。
- small_append：计时前完成祖先完整索引，再发布少量追加的后继 Revision。
- long_tool_output：冷历史包含完整工具调用与 1,500 行工具输出。

各样本使用单独迁移到 head 的 PostgreSQL 数据库、临时工作区和真实 checkpoint。保留生产索引、Curator、Patrol、编译、发布与执行路径，不替换模型响应；不改已有缓存、共享名额、后台预热或分段参数。索引预热准备与前台继续共用 normal 总预算，准备耗时在计时区间以外，用量分别记录。

## 配置与统计限制

本机默认模型为 `deepseek-flash`。本机将整理单次输出上限覆写为 65,536，高于 normal 总输出预算 50,000。本测试沿用已有 normal 索引验证口径，仅在测试内存中恢复产品 schema 默认上限 8,192；模型身份、角色专用上限和实际预算保存在每份 metadata。没有改写配置文件。这不是对本机所有覆写保持原样的测试。

提交使用进程内 ASGI HTTP transport；后续耗时还包含监督器启动、恢复和数据库轮询，是确认提交事实的观测上界。准备阶段结束后才启动监督器，防止播种期间提前开始业务；因此这组 smoke 不能当作桌面点击到渲染的正式延迟 SLO。

本次早期样本与本地后端全量回归同时运行，CPU 与 PostgreSQL 负载未受控。它们用于链路验收和阻断观察，不用于确认受控负载下的性能回归或选择分段参数。正式性能测量还需在固定负载下单独执行。

阶段数据只用于定位。`phase=preparation` 的阶段发生在提交前，不能算入本次等待；并发阶段不能相加替代墙钟时间。统计采用 nearest-rank，仅对已观测到的里程碑计算分位数，并同时给出样本总数、缺失数与完成率。每类 3 次不足以解释 P95，报告中的 P95 只是小样本顺序统计量。

## 复现

在仓库根目录的 PowerShell 执行，使用已配置模型凭据及本地 PostgreSQL：

```powershell
$env:PYTHONPATH = 'backend/packages/harness;backend/tests;.'
$env:FOCUS_SUBMISSION_REAL_MODEL = '1'
$env:FOCUS_SUBMISSION_BENCHMARK_REPEATS = '3'
$env:FOCUS_SUBMISSION_BENCHMARK_TIMEOUT = '300'
$env:FOCUS_SUBMISSION_BENCHMARK_OUTPUT = '.tmp/submission-latency-observer-v3'
python -m pytest backend/tests/test_submission_latency_real_model.py -q --tb=short
python -m backend.scripts.submission_latency_report .tmp/submission-latency-observer-v3
```

真实模型 smoke 在任何样本未到达有效输出时失败，仍保存原始报告。它不会为得到分位数而隐去失败或自动扩大预算。

前一批试跑记录另存于 `../submission-latency-preflight-2026-10-17/`，不能与本轮合并。
