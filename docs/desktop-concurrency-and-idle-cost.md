# 桌面并发与空闲成本验证

本次实现把任务列表的 5 秒刷新改为提交后状态通知和 60 秒补偿刷新，把材料监测的 2 秒全量扫描改为目录事件和 60 秒核对。索引仍使用现有服务实例内的共享名额：前台优先受理、后台最多一个调用，不抢占已开始的请求。Provider 级协调保留为实测争用后的独立改动。

## 状态通知与恢复

数据库迁移 `e36d7e8f9012` 为任务、Run 状态和待处理摘要、Loop、用户待办、工作区及材料登记变更加上 PostgreSQL 通知。通知随事务提交发布，回滚不通知，跨写入进程生效。状态更新使用字段变化条件，租约续期和 token 输出不发送通知。通知仅表达失效，不含任务正文。

每个后端进程持有一个专用 LISTEN 连接，断线以 1–60 秒退避重连；重连后使本地订阅全部失效。Electron 主进程仅订阅 `/desktop/api/task-changes/stream`，合并 250 ms 内的变化，再调用既有批量查询。这个流等待信号，不轮询数据库。连接中断、进程重启和休眠唤醒均会重新同步，60 秒刷新兜底。查询期间的新变化会安排下一次刷新。通知开关继续只控制系统通知展示。

材料监听按目录合并，使用原生文件事件，队列超过 4096 个路径时转为全量核对。监听失败使用低频扫描兜底，注册后额外核对一次以关闭注册竞态。文件事件只选择需要检查的材料，所有内容读取和恢复仍在后台串行执行。连续变化的文件每 2 秒复查稳定性，此复查不依赖 60 秒补偿周期。删除恢复、原子替换、异常隔离及摘要缓存保留。

## 采集与指标

设置 `FOCUS_PERFORMANCE_METRICS=1` 并启用 INFO 日志，可每 60 秒采集后端、Electron 各进程的 CPU 和内存。后端按操作累计 SQL 次数、执行耗时和失败数，不记录 SQL 或参数。请求、索引等待、文件检查量、首次可见 token 及取消结束记录使用 `focus_metric` JSON 行。关闭后端时补采不足一个周期的 SQL 数据。

默认模型 HTTP 客户端为每次实际 HTTP attempt 记录状态、SDK retry 序号、响应头延迟和完整响应流耗时。主 Run 和索引/结构化调用使用 invocation ID 关联；原始输入、输出、URL、请求头和凭据不进入这些指标。调用方自定义 HTTP 客户端需要自行提供同等采集能力。代理失败后的直连回退分开计数，但失败的代理 attempt 是否到达上游不可判定。

排队分为 Run accepted→claimed 和索引名额等待；Run 启动耗时、预算准入时间分别记录，不把互相包含的时间相加。首次 token 指标从 worker 开始计时，不等于从 HTTP 提交开始的端到端延迟。取消同时观察 API 请求耗时、Run 本地执行结束和索引名额释放；这些指标不证明上游已停止或用量已结算。

汇总日志：

```powershell
$env:PYTHONPATH = ".;backend/packages/harness"
python -m backend.scripts.desktop_performance_report backend.log electron.log --output report.json
```

报告按请求类别、Provider 和模型展示样本量、p50、p95、最大值。429 分母是 HTTP attempts；重试率按 invocation 分组，同时列出额外 attempts。缺失指标保持 null/空集合。比较时固定模型、输入、分段、预算、提交频率及缓存状态，预热成本包含在总成本内。小样本的 p95 仅作诊断。

## 已执行的受控验证

`backend/tests/test_concurrent_desktop_load.py` 在一次性数据库中运行真实 HTTP→Main admission→dispatch→LangGraph→checkpoint→finalizer 链路。两个长 Run 执行期间提交三个独立新要求，同时可选运行生产索引预热器；取消一个 Run 不影响另一个。Main 模型图和索引模型均受控，因此报告不得解读为真实 Provider 性能。

```powershell
$env:PYTHONPATH = ".;backend/packages/harness"
$env:FOCUS_DESKTOP_LOAD_REPORT = "docs/benchmarks/desktop-load-controlled-2026-10-17"
python -m pytest backend/tests/test_concurrent_desktop_load.py -q
```

## 上线后的真实负载矩阵

`backend/tests/test_desktop_real_load.py` 使用当前模型配置和一次性测试数据库。显式启用后会产生真实模型调用，默认每组 15 分钟、重复 3 次；空闲场景默认每次 1 小时。它创建独立任务和合成索引历史，不复用生产任务、缓存或数据库；测试结束仅取消自己创建的 Run。报告保留输入口径和限制。

```powershell
$env:PYTHONPATH = ".;backend/packages/harness"
$env:FOCUS_DESKTOP_REAL_LOAD = "1"
$env:FOCUS_DESKTOP_LOAD_REPORT = "docs/benchmarks/desktop-real-load"
python -m pytest backend/tests/test_desktop_real_load.py -q
```

可用 `FOCUS_DESKTOP_LOAD_SECONDS`、`FOCUS_DESKTOP_IDLE_SECONDS`、`FOCUS_DESKTOP_SUBMIT_SECONDS` 和 `FOCUS_DESKTOP_LOAD_REPEATS` 调整持续时间、空闲时间、提交间隔和重复次数。

矩阵包括串行单 Run、两个初始 Run、两个初始 Run 加预热和无活跃任务。单 Run 组只在前一个 Run 已结束后补充新请求；两个 Run 组按相同频率持续提交新要求，并分别采集 pending 与 running 数量及请求时间线，展示实际重叠和积压。空闲组不执行活动 Run 数量查询。后续新要求写入新测试任务，避免把同任务拒绝/排队策略混同于 Provider 争用。预热组准备一个冷的 24 条消息 Revision，重复实验使用新的数据库；这是预热突发叠加持续提交的实验，不代表整个 15 分钟始终存在预热流量。报告需结合调用时间线验证 Run 实际重叠，不能仅以“请求已受理”判断并发。空闲场景覆盖后端无材料基线；大量受保护材料及 Electron 过夜场景需在桌面实际负载下另行采样，不能从后端测试外推。

## 决策门槛与验收

功能验收：事务回滚不唤醒；外部写入能唤醒所有后端订阅；断线/重连后同步；查询期间变化不丢失；启动不重放历史完成通知；原子写入、目录失效、删除恢复和连续写入保持正确；取消后名额、监听线程和订阅连接释放。

性能验收先报告相对基线变化，不把未校准数值当作 SLA。重点排查前台 p95 反复恶化、429/额外 attempts 增加、取消迟滞、内存持续增长及空闲查询/文件检查量。优化后空闲不再出现每 5 秒任务查询或每 2 秒材料全量查询，但其他既有调度器仍可能访问数据库，必须按操作归因。

只有确认请求共享实际限流额度池且争用反复出现，才增加 Provider 级协调：按额度池共享，前台优先、后台限额且防饥饿；每次重试重新准入，退避时释放并发名额；RPM/TPM 受限时另加速率/Token 控制。当前受控实验不能证明需要该协调，因此本次未改变 Provider 调度。

## 实现验证（2026-10-18）

| 检查 | 结果 |
| --- | --- |
| 后端相关专项 | 135 个不同用例通过，包括 SQL 计数、失败记录和重复关闭释放 |
| 通知、订阅和真实 Electron 启动生命周期 | 15 项通过 |
| 后端全量 | 2750 通过、143 失败、26 跳过 |
| 桌面全量 | 433 通过、6 失败、1 跳过 |
| Standards / Spec 双轴审查 | 修复发现项后，两轴均无剩余发现 |

针对后端全部 143 个失败用例，以起始提交 `46295b78be0448ed1a0ff72fe3031d71b1f68f62` 的独立源码快照进行对照：127 个用例在基线也失败，另外 16 个在当前版本的新鲜隔离复跑中通过。3 个界面用例在基线补齐相同 Electron 依赖后同样失败；界面断言的具体位置可能随运行变化。桌面 6 个失败也全部在基线复现。仓库仍存在旧 mock、迁移回退约束、界面断言和共享测试数据的隔离问题，本次未扩大到这些修复。

完整计数及用例归类见 [validation.json](benchmarks/desktop-load-controlled-2026-10-17/validation.json)。全量运行跨越了会话中断，其耗时不作为性能结论。真实 Provider 的长时负载和桌面过夜实验没有执行，需按上述显式开关在上线后采样。
