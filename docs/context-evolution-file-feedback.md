# Context 演化策略与文件反馈

本说明对应 `add-context-evolution-and-file-feedback`，记录已实现行为、模块边界与验收结果。策略是自然语言参考，仍由 Curator 规划、Patrol 选择、Kernel 提交、现有 Loop 执行。

## 使用方式

1. 打开记忆库，选择「演化策略」或「新建演化策略」。填写适用场景、演化过程、历史继承，选择全局或工作区范围，并设置启用状态。六个预置条目与自定义条目使用同一编辑入口。
2. 在工作区 Patrol 普通输入中提出要求或纠正。策略在新的 Observation 中生效；已冻结的尝试继续使用其原版本。
3. 文件事实显示文件路径、新建/修改/删除和版本标识。具有显式产物声明的文件显示「关键产出」，用户选中或已提交引用关联的文件显示「用户引用」；这些文字与颜色不改变验证状态。
4. 点击文件行或聚焦后按 Enter，打开所选版本。预览与 Diff 在独立面板中显示；Escape 可关闭面板/候选并回到输入框。详情显示所属执行位置，区分同名文件的不同工作树。
5. 点击「引用此版本」，或在普通输入中输入 `@` 选择文件版本。选中的版本显示为可移除标签；正文仍是普通自由输入，发送目标始终是当前工作区 Patrol。
6. 引用校验被拒绝时，未被后续编辑替换的草稿自动恢复，允许移除引用后重新发送；若用户已经写下一条，失败记录保留原请求，并在当前草稿处理完后提供「编辑后重发」。网络重试始终复用原提交身份和原版本。

范围选择使用已加载任务、当前 Patrol 工作区和条目已有范围。裸 `@路径` 未选候选时只是文字。文件更新不会把已选 V1 改成 V2。打开文件与提交反馈都不会自动取消 Run 或创建 Context。

## 数据与执行路径

| 职责 | 实现 | 边界 |
| --- | --- | --- |
| 策略保存 | `memory.py`、`models.py`、增量迁移 | 在既有 Memory 上扩展类别、启停、范围、三段正文；普通记忆仍用原正文 |
| 策略冻结 | `evolution_strategies.py`、`observation_capture.py` | 按作用域冻结完整正文、memory_id、content_hash；旧记录默认为空，不静默截断 |
| 认知消费 | `decision_context.py`、`patrol_runtime.py`、`retrieval_planner.py`、`round_orchestration.py` | 单 Context 和 Portfolio 都接收冻结策略/意见；策略进入规划身份，未变 Revision 索引继续复用 |
| 原文继承 | 已有 Mission quote、Context 消息、精读和编译合同 | 不新增用户输入 EvidenceRef，不把普通讨论强制变为 Mission |
| 版本采集 | `domain_evidence/file_versions.py` | 文件写入前后观察；绑定 slot、path、attempt、Run，正文每侧最多保存 1 MiB |
| 耐久发布 | `execution_attempts/tool.py`、既有 Run outbox、journal、FactProjector | 工具结果与 Artifact/outbox 同事务提交；Run 未结算即可投影 |
| 历史读取 | `domain_evidence/file_reads.py`、事实详情 API | 通过 fact revision 定位不可变来源，不读取当前磁盘替换旧快照 |
| 普通引用 | `patrol_inputs.py`、输入 store | 同一请求的可选 file_refs，与原文共同参与 hash、回执、重试和冻结 |
| 界面 | 共用事实视图、Patrol 控制器和记忆库 | 文件行 60px、独立预览、原位更新；响应绑定工作区与请求代际 |

原始字节 hash 与脱敏展示 hash 分开。超过 1 MiB、二进制、非 UTF-8、读取失败或采集期间被修改的内容不会冒充完整快照。已确认的新文件前态为空；未观察到前态则明确说明无法生成可靠 Diff。

Shell 仅在 Git 路径集合可读取且进程清理得到确认时比较文件变化，不解析 stdout 来声称某个文件被写入。非 Git 工作区、无法可靠枚举的路径，以及没有确认清理回执的分支保留原工作区观察，不承诺逐文件历史。

文件读面沿用 `read` 或 `view_evidence`；其他通用证据仍要求 `view_evidence`。引用校验验证工作区、Fact revision、result_key、slot/path/hash。前端只显示转义后的文本，不执行 HTML/脚本文件。

任务完成核验继续使用原 `ArtifactContentProjection` 的当前文件与执行证明。历史版本缺少该完成证明，不能因可预览而通过完成检查。重要性只来自结构化产物路径或显式引用；不从普通文字猜测某文件是否为关键交付物。

## 迁移与实现收敛

迁移 `b81e2f3a4c5d` 增加 Memory 字段，以稳定身份插入六个预置；重复执行不会覆盖已有编辑。应用回滚保留字段、快照及索引，不删除用户数据。

原 outbox 对 `(run_id, event_type)` 的唯一约束限制同一 Run 只能有一个同类事件，无法承接连续文件版本。迁移将它收窄为非 `ArtifactVersion` 事件的部分唯一索引；文件事件沿用 outbox 的稳定主键，以 Run/result_key 幂等，无新事件表或消费者。

本次替换了与新格式文件路径重叠的旧结算摘要候选，防止同一操作出现两条文件事实。旧 Artifact 的结算证据生成保留，用于独立的当前完成核验及历史兼容；只读 read_file 不显示为修改。旧事实无正文时不回读磁盘伪造历史。未新增策略状态机、调度器、行动 DSL、用户输入证据类型、索引库或编译器。

## 验收记录

以下 Python 测试均使用隔离 PostgreSQL；没有改动用户工作区的实际数据库。模型输出和外部执行使用受控 fixture，不能据此声称真实模型每次都会选择相同策略或 Round 数量。

| 验收 | 结果 | 说明 |
| --- | --- | --- |
| 新增策略/文件、Live 合同与投影、物化事实、输入合同、文件工具 | 50 passed，1 deselected | 排除既有跨多版本破坏性 downgrade 用例，原因见下文 |
| 文件版本、完整反馈派生链、工作区输入 | 12 passed | 包含并发受理、暂停不恢复、首发布回滚、普通输入唤醒 |
| 派生 Kernel 关键集成 | 4 passed | 活动来源、发布原子重试、重启后单次派发、多来源四类精确证据 |
| 完整语义派生、规划、Mission、资源策略、完成内容、执行审计和物化事实 | 104 passed，1 failed | 失败为既有降级屏障；单独排除后相关物化回归通过 |
| Node 与隐藏 Electron | 47 passed | 100% 缩放/420px 六行、键盘、IME、草稿、原版本重试、异步过期、跨工作区、Live 重放、记忆 CRUD |

新增 `test_feedback_context_evolution.py` 实际经过：普通 U → Kernel/派发/启动 A → 生产 write_file 和工具账本发布 V1/V2 → A 仍 running 时读取 V1 并提交带 V1 的普通 F → A 结算 → Kernel 派发原 Context 诊断 → 发布 D → 现有 Mission/Context 精读、综合、质量合同和确定性编译 → 生产 Context 发布组件保存 B checkpoint。

断言包括原文空白保存、Mission 来源 intent_id、历史 V1 不漂移、输入重放单一身份、未自动取消 A、B 的真实历史含 U/F/D 和所引版本、不含旧方案、真实来源 revision 及 B 可运行。该场景的 B 使用生产 Context 发布组件；opportunity 的 Kernel 授权、并发发布与单次派发由上述独立 Kernel 集成验证，不把受控建议冒充真实模型决策。

八个受控规划场景覆盖六类策略：诊断失败、A 活动且再次纠正、无需派生、独立探索、竞争候选、拆分等待依赖、决定替代、综合成果。它们验证完整冻结输入、合法空/多候选及重试不重复调用；预算和多来源约束由现有资源/编译/Kernel 回归覆盖，未引入固定阶段状态。

本次发现的既有失败：

- `test_workspace_patrol_decisions.py` 的旧 helper 未提供当前认知提交必需的 Observation 身份/代际，8 个用例报 `observation_generation_changed`。其中代表用例已用本次改动前 HEAD 模块在隔离测试中复现同样失败；新链路使用仓库已有 `freeze_patrol_intent` 辅助建立完整身份，未削弱 Kernel 校验。
- `test_materialized_fact_migration_round_trips_with_existing_loop` 试图跨过 `a70d1e2f3b4c` 已有的不可逆降级屏障，仍被拒绝。本次新增迁移自身的无损降级/升级与编辑保留已验证；未移除旧屏障来使测试表面通过。

执行命令（PowerShell 先设置 `PYTHONPATH` 为 `backend/packages/harness;backend/tests;仓库根目录`）：

```text
python -m pytest backend/tests/test_evolution_strategies.py backend/tests/test_live_file_versions.py backend/tests/test_loop_live_api_contract.py backend/tests/test_loop_live_projection.py backend/tests/test_loop_materialized_facts.py backend/tests/test_workspace_patrol_contracts.py backend/tests/test_workspace_tools.py -q -k "not migration_round_trips" --tb=short --disable-warnings
python -m pytest backend/tests/test_live_file_versions.py backend/tests/test_feedback_context_evolution.py backend/tests/test_workspace_patrol.py -q --tb=short --disable-warnings
python -m pytest backend/tests/test_context_expansion_kernel.py::test_multi_source_failure_analysis_commits_exact_four_role_provenance backend/tests/test_context_expansion_kernel.py::test_read_only_expansion_retries_atomically_and_dispatches_once_after_restart backend/tests/test_context_expansion_kernel.py::test_read_only_expansion_can_be_authorized_while_source_run_is_active -q --tb=short --disable-warnings
python -m pytest backend/tests/test_complete_semantic_context_derivation.py backend/tests/test_semantic_context_planning.py backend/tests/test_mission_retrieval.py backend/tests/test_expansion_resource_policy.py backend/tests/test_completion_artifact_content.py backend/tests/test_execution_attempt_audit.py backend/tests/test_loop_materialized_facts.py -q --tb=short --disable-warnings
node --test desktop/file-feedback.e2e.cjs desktop/file-feedback.test.cjs desktop/workspace-patrol-inputs.test.cjs desktop/loop-live-projection.test.js
openspec validate add-context-evolution-and-file-feedback --strict
```

## 代码量与边界

统计以 apply 前保存的差异基线为界，包含本次新增未跟踪文件；排除预存修改及无关未跟踪文件。业务代码包含迁移与 CSS；测试含 fixture/preload；OpenSpec、说明文档单列，未把临时日志和截图计入源码。

| 分类 | 新增 | 删除 | 净增 |
| --- | ---: | ---: | ---: |
| 业务代码 | 741 | 59 | +682 |
| 测试代码 | 714 | 2 | +712 |
| 本说明文档 | 94 | 0 | +94 |

已批准 OpenSpec artifacts 共 496 行，另列为规范产物；本次 apply 更新任务完成标记及实施说明。临时日志/截图不进入仓库。

主要增长来自原先不存在的实时不可变文件快照及版本读面，其次是普通输入版本引用、紧凑详情、策略 CRUD/冻结和验证这些边界的集成测试。旧当前文件完成核验与新历史预览用途不同，因此保留；重复的结算摘要生成已在原入口收敛。

本次未执行全仓测试、真实模型试跑或生产数据库迁移，未提交/归档变更。预存 `.gitignore`、gateway、模型工厂、插件注册、工作台 E2E 与既有说明文档修改保持原样。
