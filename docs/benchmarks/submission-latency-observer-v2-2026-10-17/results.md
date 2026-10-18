# 提交到可执行：观测结果

Observer：[2]。所有累计耗时单位为秒。

P50／P95 只针对已观测到的里程碑；未观测到不计为 0。每类 3 次不足以解释 P95，不能用于选择分段参数。

| 场景 | 样本 | 五项全部到达 | 结束状态 |
| --- | ---: | ---: | --- |
| 冷构建 | 2 | 0 | timeout=2 |
| 精确缓存 | 1 | 0 | waiting_user=1 |
| 少量追加 | 1 | 0 | waiting_user=1 |
| 长工具输出 | 1 | 0 | waiting_user=1 |

| 场景 | 指标 | 观测／样本 | P50（秒） | P95（秒） |
| --- | --- | ---: | ---: | ---: |
| 冷构建 | 耐久受理 | 2/2 | 0.416 | 0.845 |
| 冷构建 | Patrol 决策提交 | 1/2 | 226.480 | 226.480 |
| 冷构建 | 派生 Context 可用 | 0/2 | — | — |
| 冷构建 | 首个关联 Run 开始 | 0/2 | — | — |
| 冷构建 | 首个关联 Run 有效输出 | 0/2 | — | — |
| 精确缓存 | 耐久受理 | 1/1 | 0.506 | 0.506 |
| 精确缓存 | Patrol 决策提交 | 0/1 | — | — |
| 精确缓存 | 派生 Context 可用 | 0/1 | — | — |
| 精确缓存 | 首个关联 Run 开始 | 0/1 | — | — |
| 精确缓存 | 首个关联 Run 有效输出 | 0/1 | — | — |
| 少量追加 | 耐久受理 | 1/1 | 0.701 | 0.701 |
| 少量追加 | Patrol 决策提交 | 0/1 | — | — |
| 少量追加 | 派生 Context 可用 | 0/1 | — | — |
| 少量追加 | 首个关联 Run 开始 | 0/1 | — | — |
| 少量追加 | 首个关联 Run 有效输出 | 0/1 | — | — |
| 长工具输出 | 耐久受理 | 1/1 | 0.550 | 0.550 |
| 长工具输出 | Patrol 决策提交 | 0/1 | — | — |
| 长工具输出 | 派生 Context 可用 | 0/1 | — | — |
| 长工具输出 | 首个关联 Run 开始 | 0/1 | — | — |
| 长工具输出 | 首个关联 Run 有效输出 | 0/1 | — | — |

| 场景 | 实际索引模式（样本数） | 阶段阻断代码（样本数） |
| --- | --- | --- |
| 冷构建 | `full`=2 | `admission/not_independent`=1 |
| 精确缓存 | `exact_hit`=1 | — |
| 少量追加 | `incremental`=1 | `admission/not_independent`=1 |
| 长工具输出 | `full`=1 | — |

准备阶段与提交后的时间分开；并发／重复投影的阶段数据不可相加替代累计耗时。
索引模式是整个样本的测量阶段诊断；导出的阶段未将模式绑定到目标 source_revision_id，因此不能单凭模式表验证目标历史的缓存路径。
阶段阻断按样本去重；一次样本可能出现多个代码。未派生可能是策略选择或阶段失败，不能全部解读为耗时超标。

配置、原始样本、缓存模式及用量见 [summary.json](summary.json)，测量边界与复现见 [README.md](README.md)。

Observer v2 follows pending/deferred causal_chain inputs and derived-Context Runs only. It cannot recover later Mission-source-only decisions or Primary continuation Run timings. The updated v3 probe adds those paths; do not mix v2 samples with the v3 all-Run scope.
