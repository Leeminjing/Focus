# Focus

**human in the contexts · 上下文手术**

> 一个基于 LangGraph 的开源、本地优先 Agent。它的唯一信念，是 "human in the loop" 里那个 *in*——从执行循环抬升到上下文本体：**人参与在 contexts 里，并直接在它们上面做手术。**

> 经过筛选的精确的上下文优于臃肿但缓存命中率高的上下文，并且随着 run 的轮次增长，前者的花费反而可能低于后者。
>
> A carefully curated, precise context is better than a bloated context with a high cache hit rate, and as the number of run iterations increases, the former may actually become less expensive than the latter.

> **普通 Agent Loop 迭代 Prompt。Focus 迭代 Context。**

```
                    Focus
                      │
          ┌───────────┴───────────┐
          │                       │
      Agent Focus             Human Focus
          │                       │
   Context Surgery              Patrol
          │                       │
Prevents Context Drift    Prevents Attention Drift
          │                       │
          └───────────┬───────────┘
                      │
         Patrol can perform surgery
              on the user's behalf
                      │
                 Focus on Task
```

## Focus Agent Loop：让 Context 替长期任务持续进化

多数 Agent Loop 只解决“怎样再运行一次 Agent”：上一轮结束，外部脚本再发一条 Prompt，Agent 带着同一段越来越臃肿的历史继续。运行次数增加了，但任务方向、信息边界和错误惯性仍由上一轮输出决定。

Focus 改变的不是重试次数，而是**下一轮 Agent 所处的世界**。每轮结束后，Patrol 观察任务与 Context Portfolio，判断应该沿用、策展、派生、合并、暂停还是结束，再由确定性 Kernel 校验并提交下一代 Portfolio。

```text
用户给出目标与授权
        ↓
Agent 在一个 Context 中执行
        ↓
Portfolio Patrol 观察结果与任务全局
        ↓
继续当前 Context / 策展历史 / 派生多个 Context / 合并已有结果
        ↓
Patrol 代表用户写入普通 HumanMessage
        ↓
一个或多个 Agent 在各自 Context 中继续
        ↓
Patrol 再观察、再选择，直到完成或等待用户
```

这不是 `AI → AI → AI` 的自我续写。它始终保持 `HumanMessage → Agent → HumanMessage → Agent` 的控制节奏，只是后续 HumanMessage 可以由用户预先授权的 Patrol 生成。

### 为什么这不是又一个 `/loop`

这套 Loop 直接建立在 Focus 已有的两项原生能力上：Derived Context 能改变模型下一轮看到的过去，Patrol 能代表用户持续行使 Context 控制权。缺少其中任何一个，系统都只剩“再发一条 Prompt”。

| | 普通外层 Agent Loop | Focus Agent Loop |
|---|---|---|
| 下一轮输入 | 根据上一轮结果拼接 Prompt | Patrol 生成 delegated HumanMessage |
| 历史 | 继续累积，或每轮全部清空 | 从不可变 Revision 精确策展 Derived Context |
| 任务结构 | 一条线，或若干彼此孤立的 attempt | 持续演化、可分可合的 Context Portfolio |
| 并行 | 多跑几次，再选一个结果 | 每个 Lane 拥有不同目标、历史与 workspace |
| 控制权 | Agent 或外部脚本推进 | 用户是根权力，Patrol 是唯一可撤销代理 |
| 完成判断 | Agent 自报完成，或脚本看退出码 | 独立证据、Completion Guard、必要时等待用户 |

普通 Loop 设计下一条指令；Focus 同时设计**下一条指令、下一轮历史、下一组执行世界以及它们之间的血缘**。这正是 Derived Context 与 Patrol 结合后才出现的能力。

### One Patrol, one evolving Context Portfolio

一个 Loop 只有一个拥有控制权的逻辑主体：**Portfolio Patrol**。它是用户唯一的、可撤销的委托权力持有者，持续管理的不是一条聊天记录，而是一组会演化的任务认知 Lane。

```text
Portfolio R12                     Portfolio R13

Implementation R8  ────────────→ Implementation R9
Testing        R5  ────────────→ Testing        R6
Requirement    R3  ────────────→ Requirement    R3  （无需更新）
Architecture   R6  ────────────→ Architecture   R7
Scope Drift    R2  ────────────→ 退出
                                  Release        R1  （新建）
```

这里的“一”指一个 authority holder，不是只允许一次模型调用。Patrol 可以自己完成观察、判断与策展；需要并行构造 Lane、建立语义索引、基于证据进行合成、独立评估质量或验证完成状态时，才调用临时 Worker。

> **Patrol owns judgment. Workers are optional cognitive tools.**

Worker 只返回类型化的候选、semantic unit、合成 claim 或验证 verdict。每种角色只能读取自己的冻结输入，不能改变 Portfolio、不能启动 Run、不能写入 delegated Human channel。无论 Worker 多强，最终都遵守同一条原则：**Workers return. Patrol commits.**

### Patrol 决定往哪里走，Derived Context 决定带着什么过去

这是 Focus Agent Loop 与普通外层循环的根本差异。

| 控制问题 | Focus 的回答 |
|---|---|
| 下一轮往哪里走？ | Patrol 根据目标、结果、失败、偏航和新机会作出判断。 |
| 下一轮带着什么过去？ | Derived Context 从一个或多个不可变 Revision 中选择、重组并编译历史。 |
| 谁能把判断变成系统状态？ | 只有当前 Portfolio Patrol 能向 Kernel 提交 decision intent。 |
| 用户离开后谁行使控制权？ | Patrol 在可撤销的 `LoopDelegationGrant` 范围内代表用户。 |

Context identity 可以长期存在并彼此影响；真正保持无环的是不可变的 Revision 血缘。于是 Architecture 可以影响 Implementation，Implementation 的新结果也可以生成 Architecture 的下一版，而不会伪造或覆盖历史。

```text
Architecture R4
      ↓
Implementation R7
      ↓
Architecture R5
```

Portfolio 是索引，不是超级上下文。Patrol 的普通控制观察仍保持有界，只读取每个 Lane 的 purpose、revision、freshness、summary、fingerprint 和最近结果。

但自动 Context 派生不会再基于截断的尾部消息预览进行规划。系统会把每个不可变 source Revision 完整索引为 protocol-safe segments 与有原文依据的 semantic units；Planner 从紧凑的 Portfolio catalog 出发，在授权索引中检索候选，并在生成 `WorkContextSpec` 前执行有预算的精确读取。完整历史始终可达，但不会被整段塞进每一次模型调用。

### 多对一、多对多持续策展

Focus 不把 Derived Context 限制成“一棵只能向外分叉的聊天树”。Patrol 可以从多个来源 Revision 策展一个新 Lane，也可以把同一组来源投影成多个拥有不同历史与目标的 Lane。

```text
{ Goal, Contract, Implementation, Test failures }
                         ↓
                 Portfolio Patrol
          ┌──────────────┼──────────────┐
          ↓              ↓              ↓
 Implementation     Failure Analysis   Architecture Review
 继续实现所需历史     三轮失败与日志       目标、约束与关键决策
```

实现 Context 不必携带全部调试噪声；测试 Context 不必继承无关架构讨论；反方审查 Context 也不必接受当前实现已经形成的思维惯性。每个 Agent 只看到完成自己任务所需的过去。

当多个 Lane 产生结果后，Patrol 可以继续其中一个、淘汰一个、从多个结果策展新 Context，或把任务重新拉回 Task Contract。下一轮不必沿着上一轮 Agent 自己建议的方向前进。

### 委托式 HumanMessage：人可以离场，人的权力仍在 Loop 中

Patrol 发出的控制指令进入模型时，就是普通的 `HumanMessage(id, content)`。正文、`additional_kwargs` 与 system prompt 都不包含 Patrol 标签、授权字段或“synthetic human”提示。

真实来源保存在模型上下文之外的 provenance 记录中。用户在 Desktop 中能看见哪条消息由自己输入、哪条由 Patrol 根据哪份授权生成；模型只接收干净的 human role 消息。

```text
模型看到：HumanMessage("暂停修改代码，只分析过去三轮为什么失败。")

系统记录：actor=patrol · authority=delegated · grant=... · patrol=...
```

因此 `HumanMessage` 表示的是 human authority domain，而不必等同于“用户此刻亲手敲下”。用户始终是根权力；Patrol 只是可撤销代理。用户一旦回来输入新要求，新的直接 HumanMessage 会推进目标与授权版本，使尚未提交的 Patrol 决策失效。

### 长程任务不会被“继续”两个字拖死

- **发生偏航**：从更早或更干净的 Revision 派生新 Context，重新对齐 Task Contract。
- **陷入重复**：停止机械重试，建立只分析失败模式的 Lane，再据此恢复执行。
- **发现支线问题**：创建独立 Side Context 调查，主线继续，不把整段支线历史强塞回来。
- **需要多视角**：让实现、测试、需求和架构 Lane 并行推进，再由 Patrol 选择与组合结果。
- **无法证明完成**：独立 Completion Verifier 提供证据；证据不足时进入 `waiting_user`，绝不擅自宣布成功。

> **The human does not need to remain present in every iteration; their authority does.**
>
> **人不必守在每一轮执行旁边，但人的控制权始终留在 Loop 中。**

Derived Context 让 Focus 能重写下一轮的过去；Patrol 让这种能力持续、自主而可治理。两者合在一起，Agent Loop 不再只是重复调用模型，而成为一台持续演化任务认知状态的 **Context Evolution Engine**。

## Windows 安装

先准备 Git、Python 3.11+、Node.js 22.12+ 与 Docker Desktop，然后在 PowerShell 中执行一次：

```powershell
irm https://raw.githubusercontent.com/Leeminjing/Focus/main/install.ps1 | iex
```

把模型 API Key 写入 `%USERPROFILE%\.focus\.env`，以后可在任意目录使用同一组命令：

```powershell
focus          # 启动 Focus
focus update   # 将受管理的已跟踪代码同步到 GitHub 最新 main
```

安装器把程序代码放在 `%USERPROFILE%\.focus\app`，托管的 Python 环境放在 `%USERPROFILE%\.focus\runtime`，命令入口放在 `%USERPROFILE%\.focus\bin`。持久化配置、插件、用户和 PostgreSQL 数据位于受管理 Git 工作区之外；选中的工作区也应放在 `.focus\app` 之外。`focus update` 会有意丢弃 `.focus\app` 内已跟踪文件的本地改动；开发请使用另一个 clone。

## macOS 安装

先准备 Git、Python 3.11+、Node.js 22.12+，并启动 Docker Desktop，然后在终端执行一次：

```sh
curl -fsSL https://raw.githubusercontent.com/Leeminjing/Focus/main/install.sh | sh
```

把模型 API Key 写入 `~/.focus/.env`，打开一个新终端后使用：

```sh
focus          # 启动 Focus
focus update   # 将受管理的已跟踪代码同步到 GitHub 最新 main
```

macOS 安装器同样使用 `~/.focus/app`、`~/.focus/runtime` 和 `~/.focus/bin`，并把命令目录加入当前 shell 的用户配置文件；整个过程不使用 `sudo`。当前提供的是未签名、从源码托管运行的 Electron 分发方式，不是经过公证的 `.app`，因此 macOS 可能显示安全提示。更新只替换程序目录内的已跟踪文件，不影响 `~/.focus/app` 外的持久数据。

---

## 这个 "in" 是参与，不是坐标

"Human in the loop" (HITL) 从不意味着一个人物理上*待在*循环里。它命名一种状态：在自动化流程中，人**作为决策者参与其中**，在机器不该独自做决定的地方介入。那个 "in" 是**参与**，不是空间介词。

"Human in the contexts" 用的是同一个 "in"，只是高了一层。

- **HITL / in the loop** —— 人参与的是**执行层**：工具调用、输出流、哪些动作被批准。
- **Focus / in the contexts** —— 人参与的是**上下文本体**：模型赖以推理的基底。由人决定哪个上下文成为合同、哪些消息被折叠、哪个上下文被分叉、一个平行 Agent 醒来时带着什么、哪些记忆跨会话活下来。

"上下文手术" 是同一句话换了一只手说——参与的那个人，握着手术刀。

> **人参与在 contexts 里，并在它们上面做手术。**

---

## Patrol：委托的参与

"Human in the contexts" 并不意味着人必须手动操作每一次上下文变换。人拥有**决定权**；**Patrol 可以代表人来执行这个操作**。

- **直接参与** — `Human → Context Surgery`
- **委托参与** — `Human → Patrol → Context Surgery`

> **控制权可以委托，而不必交出所有权。**

"Human in the contexts" 定义了用户对 context 的权威。**Patrol 让这份权威变得可委托。** 它的第一个落地实例是**上下文策展**：

```
Main Context
     │
     ▼
   Patrol
     │
     ├─ reads the committed context
     ├─ removes noise
     ├─ preserves goals / constraints / decisions
     ├─ curates a new context
     ▼
Derived Context
     │
     ▼
Agent continues from a cleaner context
```

快捷策展正是 Patrol 作为 "Human in the contexts" 快捷键的第一次落地。

---

## 公理的内核

一条规则在整个系统里自我复制：

> **在每一处"上下文变换"的边界，把模型的不确定性关进一个可机械校验的窄壳里——然后由确定性代码负责授权与隔离，由人负责批准与书写；用消息作为唯一事实源，用 checkpoint 命名空间当作身份，用墓碑保留来源，并在代码无法确定时交还给人，而不是猜测。**

---

## 架构

一个 FastAPI gateway 通过一条 `POST /api/threads/{thread_id}/runs/stream` SSE 通道驱动 Focus；桌面壳以同源加载，无 CORS。

```
┌────────────────────────────── FastAPI Gateway ──────────────────────────────┐
│  router (access)  →  services (orchestrate)  →  worker (execute)             │
│  AuthMiddleware · RunManager · StreamBridge · checkpointer · store            │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │ SSE (metadata / tokens / events / interrupt /
                                   │  commitment_messages / error)
                                   ▼
                     ┌──────────────────────────────┐
                     │   focus agents (LangGraph)     │
                     │   make_lead_agent ──────────►   │
                     │   (唯一装配腰)                 │
                     │   middleware + tools + prompt  │
                     └──────────────┬───────────────┘
                                    │  run_agent (唯一执行脊柱)
                                    ▼
                        ┌───────────────────────┐
                        │   Patrol (委托)        │
                        │  跨上下文机制的       │
                        │  委托执行者            │
                        └──────────┬────────────┘
            ┌───────────────┬──────┴────────┬────────────────┐
            ▼               ▼               ▼                ▼
         压缩门         context 分叉       记忆库           承诺层
         手术门         DAG (f15)         <memory>          九阶段
```

- **存储**: PostgreSQL + asyncpg checkpointer（跨重启持久）+ LangGraph Store（`async_postgres`，关闭向量）。
- **流式**: 进程内 `StreamBridge` → SSE；`RunManager` 管理 run 状态；checkpointer 提供 rollback 快照。
- **身份**: 每一个 agent 只是 `(thread_id, checkpoint 命名空间)`——`patrol:{id}`、`{thread_id}:commitment`——同一个引擎，不同的房间；所以分叉、隔离、恢复都只是命名空间。
- **前端**: 零依赖静态 UI，由同一个 Python 进程直接服务（手写 vanilla JS + design-token CSS；一个 vendor 的 markdown 库）。无 npm UI 依赖、无构建、无框架。

---

## 核心机制

上下文操作按**两组**机制实现，而不是平铺的五个并列。**这些上下文操作是基底；Patrol 是能代表用户执行它们的委托执行者。**

### Context operations

每一个操作都是人参与的地方，也都是"人决定留下什么"的地方。

#### 压缩机制

主 Agent 在每次模型调用前估算 token 用量。当用量超过 `context_window × threshold_ratio` 时，图 interrupt 暂停并询问你——它从不自行修剪。

- 你划定一个或多个消息范围（甚至可以拆散一个 tool-call 组，由代码事后修复协议）。
- 系统为每个范围生成候选摘要；你接受、编辑、重新生成、或取消。被留下的是*你的*文字。
- 你也可以直接把某个范围删除（墓碑），而不是总结它。
- 确认后 messages 重写：每个范围变成一个压缩块（一个携带来源原文 + `block_id` 的 `HumanMessage`，随 checkpoint 持久化）；删除变成空墓碑。
- `wrap_model_call` 在每次模型调用前剥离 compression 元数据，来源原文永不进入模型。
- 压缩块可展开、重新编辑、再压缩、撤销；恢复走 resume 通道。存在待确认压缩请求时，新的主运行返回 409，直到解决。

#### 派生 contexts 机制

context 不是单扇窗口；你可以**派生**一个。从同一个工作区的一个或多个已提交 checkpoint，派生出一个新 context。

- 系统保存两份：`authored_messages`（你写的）与 `execution_messages`（模型运行的合法投影）。
- 投影器只**增加结构**——它用合成占位修复悬空 tool call。任何会改写你文字的处理都变成 `approval_required`，即显式、绑定哈希的 `accept`/`reject`，绝不静默改写。
- 每个派生 context 拿到一个**全新** `thread_id`；投影写入新 checkpoint，从不动父级。
- contexts 长成一棵带深度与血统的树，可追踪、合并、分叉、归档、删除（有后代时以墓碑保留）。

#### 承诺层

开工之前，Focus 让你先**立一份合同**——而这份"立约"的机械故意不是 LLM。

- `/commit <指令>` 触发九阶段状态机（目标 / 需求与矛盾 / 优先级 / 输入 / 版本 / 官方知识 / 合同 / task_contract / 最终消息）。
- Supervisor 是确定性 `StateGraph`，伪造一条 `AIMessage`（合成 `tool_call` 指向 `delegate_with_review`）交给 `ToolNode`。LLM 只在工具函数体内（Worker + Evaluator）运行；哪个阶段、下一步做什么、何时停——那是纯代码。阶段顺序锁死：`expected = stage + 1; stage != expected → invalid_stage`。
- Worker 提议；独立 Evaluator 审核；确定性校验器检查结构。三轮未过把决定交还给你。
- 阶段 3/5/6/7（以及条件的 2/4）由你亲手批准或修订。
- 签字后，合同**原位替换**那条 `/commit` 消息（同一 message id），位于隔离命名空间 `{thread_id}:commitment`；`/commit` 之前的对话原封不动。
- 因为 Supervisor 是代码，它无法被 prompt injection 说服跳步或重排；LLM 的触手被关进一个 schema 收口的盒子。

#### 记忆库

记忆不是模型的工作，而是**你**的策展。没有什么是自动捕获或自动回想的。

> 记忆的最终治理权属于你。策展可以直接做，也可以委托给 Patrol。

- 你从某个完整会话、某几条（非连续）消息、某段文字、或你自己的话里取材——任意组合。
- 你把它压成一份 `complete` 纵览或几条独立 `segmented` 笔记，然后**自己改定草稿**；被保存的是*你的*版本，不是模型的。
- 每条记忆带 `source_snapshot` 与逐段 `source_ref`，因此始终可溯源、可重新生成。
- 开启新会话前，你挑选哪些记忆带走，以 `<memory>` 块注入系统提示词——只作用于主 Agent。

### 委托操作：Patrol

Patrol 不是上面这些机制的第五种——它是**委托执行者**，能代表用户执行这些机制。

上面的上下文操作是基底；Patrol 可以代表用户操作压缩、派生、记忆、策展……它有双重角色：

1. **注意力隔离** —— 把支线任务委托出去，不打断用户的主线任务。
2. **上下文操作** —— 代表用户执行上下文操作：策展、派生、压缩、整理记忆……

下面的「独立房间」是它*怎么工作*，不是它*是什么*。Patrol 真正的定义是：**用户的、跨各种上下文机制的委托式上下文操作者（delegated context operator across context mechanisms）**。

- 从主 Agent 最近已提交 checkpoint 深拷贝出冻结草稿，然后自由编辑它的 `system_prompt`、历史、最后一条消息。
- 投放是**幂等**的（`deployment_id` + 唯一约束）：重复点击绝不产生副本。
- 它在自己的命名空间（`patrol:{id}`）运行，与你的主线并行、绝不打断它。
- 它的结果**永不自动注入**你的主线。你想读时再读，经 `list_patrol_agents` / `read_patrol_agent_history`。
- 它有独立生命周期——取消它的 run、按最初冻结输入重试、或追加一条消息继续对话。
- **空间小兵**把同一想法钉在某个位置：把一个 patrol 放到文档或页面上的某个坐标，它从那个点向外观察；位置就是它的身份。DOCX 编辑需要显式的读/写授权。

---

## Context 操作：人与代码的分工

每一处被治理的 context 操作，都把模型的自由度坍缩成一个窄产物。代码拥有该产物的**授权与隔离**；LLM 只拥有其中的**内容**；人拥有**决定权**。Patrol 是一种**执行模式**，可以代表用户执行以上任何一种操作。

| 操作 | Context 产物 | 代码拥有 | LLM 拥有 | 人拥有 |
|---|---|---|---|---|
| **压缩** | 压缩/墓碑化消息 | 校验范围、修复协议、剥离元数据、携带来源 | 提议摘要 | 划范围 · 写或改写 · 删除 · 撤销 |
| **派生 context** | authored / execution 投影 | 编译投影（只增）、哈希绑定 accept/reject、全新 thread_id、血统 | —（人写的） | 撰写消息 · 接受或拒绝 |
| **承诺层** | 任务合同 | Supervisor（代码）、校验器、原位替换、命名空间 | 提议阶段内容 | 批准 / 修订每个阶段 |
| **记忆库** | 记忆（complete/segmented）+ `<memory>` 块 | 解析来源、切文字、渲染块、`_safe_attr` | 提议压缩草稿 | 挑来源 · 编辑草稿 · 决定带什么 |
| **patrol** *（操作者，而非操作）* | 它操作的那个 context 产物 | 幂等投放、命名空间隔离、不自动注入、读取工具、策展管线 | 干活 | 重写草稿 · 投放 · 选择读 · 批准策展 |

---

## 能力地图

- **Focus Agent Loop**: Patrol 持续治理 Context Portfolio；Derived Context 决定下一轮携带什么过去；delegated HumanMessage 决定下一轮往哪里走。支持多对一、多对多策展、并行 Lane、原子发布、独立完成验证与用户随时接管。
- **承诺层 (Commitment)**: `/commit <指令>` 触发 9 阶段；Worker–Evaluator 审核；阶段 3/5/6/7 固定人工暂停；输出 `task-contract`。Context7 仅为该流程懒加载。
- **压缩机制**: 触发阈值 + 用户划范围 + LLM 摘要草稿 + 墓碑删除/撤销/重开；来源随 checkpoint 保留，模型看不到。
- **派生 contexts 机制**: 从单/多父 checkpoint 派生；`authored vs execution` 分离；哈希绑定 accept/reject；lineage/depth/tree；全新 thread_id。
- **patrol 机制**: 用户的委托式上下文操作者 —— 注意力隔离（支线任务不占主线）+ 上下文操作（代表用户策展/派生/压缩/整理记忆）；实现为冻结草稿 + 全量重写上下文 + 幂等投放 + 独立命名空间 + 结果不自动注入。**快捷策展**为你派生一个更干净的 context。
- **记忆库 (Memory)**: 多来源（会话/消息/文字/手输）、complete/segmented、人工编辑后保存、`<memory>` 注入新会话（仅 main）、跨会话持久化。
- **插件 (Plugins)**: 接口目录（tool/hook/service）+ 依赖注入注册表 + 单一桥接中间件 + 语言中立 stdio 远程协议；可携带桌面 API 路由与前端资源。

---

## Agent Loop 执行内核

前面的产品行为由四层硬边界实现：Delegation 定义权力，Patrol 形成判断，Kernel 校验状态转换，Run Orchestration 执行。模型负责认知内容，但没有任何一层让模型直接写入权威状态。

```text
用户（根权力）
  └─ 可撤销的 LoopDelegationGrant
       └─ 一个 Portfolio Patrol（唯一委托权力持有者）
            ├─ 观察有界的 Portfolio frontier
            ├─ 自己判断继续、策展、派生、合并、等待或停止
            ├─ 必要时调用角色受限的索引、规划、合成与验证 Worker
            └─ 提交一份严格 decision intent
                 └─ 确定性 Kernel 校验并提交
                      ├─ 原子 Portfolio publication
                      ├─ 普通 HumanMessage 指令
                      └─ 并发安全的 Agent Run
```

Patrol 拥有判断权；Worker 只是可选外脑。Worker 只能返回类型化产物，没有任何状态提交端口；Kernel 是唯一 commit 边界。核心不变量是：**多 reader、多 advisor、多 candidate producer，但每个 Portfolio 只有一个权威 publisher**。

### 有证据约束的语义 Context 派生

自动派生是一条 fail-closed 的阶段流水线，不再是关键词路由或最近消息切片：

```text
完整不可变 Revisions
  → protocol-safe semantic indexes
  → Portfolio 范围内的有界检索与精确读取
  → WorkContextSpec 规划与语义 reconciliation
  → 精确的多源 evidence resolution
  → claim 级 dossier synthesis 与 grounding verification
  → minimality · sufficiency · coherence Quality Gate
  → 确定性 Context compilation
  → Kernel admission 与 commit
```

每个阶段都由 semantic identity 与 source hash 串联。投影 statement 可以是忠实改写，但每个 confirmed unit 都必须携带冻结原文中的精确 support spans，并经过独立的 `supported | unsupported | unknown` 判定。单个可选 unit 失败会进入 rejection ledger，确定性 segment coverage 仍使 Revision index 保持 ready。Index artifact 记录被接受投影的 unit ID 和精确 fallback segment ID；descriptor 暴露两者数量，并将只有 segment coverage 的结果标为 `degraded`，而非 `complete`。Revision identity、content hash、scope 与 stale-source 错误仍整体阻断。Required evidence 必须解析到精确 source unit；tool call/result 必须保持闭合；相互冲突的 WorkSpec 不会被静默合并；无原文支持的 claim 或任一 unknown 质量结论都会阻止编译。系统持久化模型调用尝试、实际 token 用量、阶段耗时、schema/projector 版本、输出、重试与稳定失败码，用于审计和重放。旧 semantic cache 不会被重新解释；版本化 cache key 会触发确定性重建，immutable source Revision 不被改写。

同一条派生链可以运行在 observe-only shadow mode，只生成对比产物，不创建 Context，也不改变 Portfolio。运维可设置 `FOCUS_LOOP_AUTOMATIC_CONTEXT_EXPANSION_WRITES=false` 关闭自动派生写入；规划与诊断仍会继续，但权威写入路径保持关闭。

### Tool 协议修复与单来源恢复

Checkpoint authored history 是不可变审计事实。Run settlement 发布可运行 Revision 前，共享 tool-exchange compiler 会另外生成 provider-facing execution view。已证明 interrupted/cancelled 的调用只会获得一个确定性的 `status=error` ToolMessage，保留原 call id 与 tool name；repair manifest 记录 source Run、source Revision、原因、compiler version 与 synthetic message。归属证明包含精确来源 checkpoint：后续 Provider 故障不能把继承的旧调用认作本次中断。无法证明原因的缺失 call/result 保持 `approval_required`，绝不伪装为成功。

当旧 Context 或 Provider 拒绝后的 Context 无法继续，但某一个权威 Revision 存在安全且保留证据的子集时，Loop 会持久化 `ContextRecoveryOpportunity`。可信恢复编译器只隔离没有部分结果的终端未闭合交换，保留来源引用，并明确记录工具副作用未知；非终端或部分完成的交换需要人工批准。重复发现保留机会首次记录的到期时间。Patrol 只能选择 `recover_context(opportunity_id=...)`，可信服务负责构造单来源 `CreateLanePlan`。Kernel 重新校验 source frontier、goal、workspace、authority、grant、compiler version、expiry、消费状态和当前 source Revision；opportunity 消费与 Lane/Portfolio publication 位于同一事务，因此 stale 或 replay 不会发布部分状态。若协议合法性与必要证据无法同时保留，observation 会给出明确等待原因，指出缺失证据或所需批准。

追加迁移为 `6a7b8c9d0e1f`。回滚只删除尚存的 recovery opportunity 表，不改写 authored checkpoint、已记录的 repaired Revision、既有 Context lineage 或已发布 Portfolio。若部署需要跨该边界回滚，应先禁用新 reader/action，再执行 downgrade。

### 原子 Portfolio 发布

每一代 Portfolio 先冻结 source frontier，再并行准备候选 Revision。全部候选通过 schema、血缘、权限、projection 与 writer ownership 校验后，系统才原子切换所有 Lane 指针并发出 `PortfolioPublished`。

只要一个候选失败，整代 Portfolio 都不会部分可见，Agent 也不会在“Implementation 已更新、Testing 仍停在上一代”的混合世界中运行。

### 权限与消息来源

委托指令进入模型时严格只有 `HumanMessage(id, content)`。Patrol 身份、grant、authority 和审计信息保存在外部 provenance 表，不会写进正文、`additional_kwargs` 或 system prompt。

Desktop 历史仍能向用户区分直接与委托 HumanMessage。Delegated Human channel 只接受当前有效 grant 的持有者写入；用户输入会立即取得更高优先级。

### Workspace 并发与采用

Workspace 使用带 fencing token 的单 Writer/多 Reader lease。多个获授权 Writer 从同一个干净 Git 基线进入 Lane 专属 worktree；结果保持隔离，直到 Patrol 针对当前权威 revision 明确采用其中一个。非 Git 或脏工作区退化为单 Writer。中断 Reader 只有在 fingerprint 证明未变化时才能重试，Writer 永不盲目重试。

### 完成不是自证

完成不能由 Patrol 自证。独立 Completion Verifier 逐条返回验收证据；Patrol 判断是否请求结束；Completion Guard 再检查证据新鲜度、未决 gate/Run、Portfolio 完整性、workspace adoption 与最终路径。任何 unknown 或 conflict 都进入 `waiting_user`。

### Patrol 可以自主压缩，但用户始终保有定义权

新建 Loop 时默认显式勾选 **“允许 Patrol 自主压缩 Context”**。这份权力可恢复、可撤销：取消勾选会创建或收窄为不含压缩权力的 grant；用户直接发消息、手工压缩、覆盖目标、修改授权、暂停/停止 Loop，或发布更新的 Context revision，都会让旧候选立即失效。

自治压缩分成四个硬边界。`CompressionGate` 只产生稳定 interrupt；Patrol 先查看不含消息正文的有界 manifest，只能请求受授权的精确范围，既有摘要能力再生成不具权威性的 candidate；Kernel 对 Loop/grant/goal/round、Context revision、checkpoint、frontier、policy、保护锚点、预算、过期时间和 token 减量全部做 compare-and-set 校验，然后原子提交唯一 resolution；带 lease 与 fencing 的 coordinator 最后沿用手工压缩同一条 `DesktopService.resume_run → execute_prepared_run → RunLifecycleFinalizer` 执行脊柱恢复 LangGraph。

`apply_compression_ranges` 仍是手工、快捷和自治入口唯一的消息变换编译器。Checkpoint 保存完整序列化 source，用户可恢复原文；下一次 provider 请求只看普通摘要正文，candidate id、Patrol 身份、grant、resolution 审计元数据和删除墓碑都不会进入模型上下文。

Loop Console 展示 `prepared → accepted/committed → resuming → applied`，也展示 `expired`、`superseded`、`failed`，并串联目标 Context、范围、预计/实际 token 减量、Run、checkpoint 和最终 revision。进程重启时由数据库事实与过期 lease 决定继续恢复、观察已存在 Run、淘汰陈旧 frontier，还是等待用户；迁移回滚只删除自治压缩的 policy/candidate/resolution 结构，不会改写已经存在的压缩 checkpoint 或恢复源。

### 可观察、可接管、可恢复

在 Desktop 任务页打开 **Agent Loop**，填写目标、Task Contract、验收条件和预算后授权 Patrol。Loop Control Console 把持续演化的 Context Portfolio 与当前选中 Context 的完整会话放在同一个页面：每个节点都有主题、职责、revision、Run 状态和证据计数；稳定的多来源连线展示当前世界如何派生。点击节点即可查看完整 Human/Assistant/Tool 记录，来源审计始终位于模型正文之外；事实抽屉则按单个或全部 Context 展示可追溯 Run、工具结果与保守统计的测试事实。

用户有三条明确的介入路径：直接向选中 Context 发送 HumanMessage；向 Patrol 表达只针对该 Context 的意见；或调整整个 Portfolio 的布局。给 Patrol 的意见会作为外部用户意图持久化、审计，并进入下一次冻结 observation，绝不会被偷塞进执行 Agent 的消息历史。

所有 Loop 事件都通过 cursor 可重放的 SSE 输出。用户直接发送新消息或使用“用户接管”时，系统推进 goal 与 authority revision，并使基于旧授权且尚未提交的 Patrol 工作失效。

当前运行限制：并行隔离写入需要 Git 且基线干净；扩大访问范围等不可委托 gate 永远等待用户；旧 Loop grant 不会被静默升级为自治压缩，只有根用户明确授予 compression gate、closed capability 与版本化 policy 才能启用；Context revision 历史和未采用 Lane 会为审计保留，直到生命周期清理策略允许删除。

---

## 技术栈

- **语言/框架**: Python · LangChain · LangGraph（`create_agent`）。
- **服务**: FastAPI (gateway) · 进程内 `StreamBridge` · SSE。
- **存储**: PostgreSQL (asyncpg) · Alembic · LangGraph Store（`async_postgres`，关闭向量）。
- **模型**: OpenAI 兼容（默认 `deepseek-v4-flash-vision-exp`，经 `focus.models.deepseek`），经 `use: module:Class` 可插拔。
- **外部能力**: MCP（Context7 文档、Playwright 浏览器）；vendored vanilla-JS 前端。
- **配置**: `config.yaml` · `extensions_config.json` · `plugins/<name>/plugin.json`。

---

## 开发环境

**要求** — Python ≥ 3.11（需可用 `alembic` 与 `uvicorn`）、`git`、`docker`（用于 `desktop/compose.yaml` 内置的 PostgreSQL）、Node.js ≥ 22.12.0（Electron 43 的硬性要求，更低版本会在其安装阶段失败）、以及配置在 `OPENAI_API_KEY` 里的 OpenAI 兼容 Key（DeepSeek）。

**配置** — 密钥与选择经环境变量提供，结构经配置文件提供。

| 变量 | 必填 | 说明 |
|---|---|---|
| `OPENAI_API_KEY` | 是 | 模型条目的密钥来源 |
| `FOCUS_MODEL` | 否 | CI / 一次性覆盖：取生效目录中已存在的条目名；写成不存在的名字会在启动期失败，错误信息会列出全部可用条目名 |
| `FOCUS_DATABASE_URL` | 否 | 覆盖数据库连接 |

变量可写入 `.env`（`cp .env.example .env`），也可用操作系统环境变量提供（优先级更高）：Windows `setx OPENAI_API_KEY "sk-..."`，macOS/Linux `export OPENAI_API_KEY=...`；两者都需新开终端生效。

**模型怎么配** — 打开桌面应用的「设置 → 模型」：新增/编辑/删除条目、指定默认与策展默认、录入或轮换密钥、测试连接，保存后立即生效，不需要重启或用终端。手动编辑配置文件同样受支持。

配置按两层由低到高解析，高者覆盖低者：

1. 文件层 `<cwd>/config.yaml` —— 这份程序自带的默认（安装版即 `~/.focus/app/config.yaml`，升级会整体替换）；
2. 用户偏好层 `~/.focus/config.yaml` —— 设置面板写入，升级不丢，**优先于文件层**。

环境（`.env` 与进程环境）只经两条通道参与，**不是**通用的逐键覆盖层：配置值里的 `$VAR` 引用在使用期解析；以及有文档化覆写键的两项 —— `FOCUS_MODEL`（默认模型选择）与 `FOCUS_DATABASE_URL`（数据库连接），它们优先于上面两层。其它配置项不会被同名环境变量改写。

`models` 按条目 `name` 逐条合并：用户偏好层声明的条目覆盖同名默认条目，未声明的默认条目保留，删除用显式的 `removed_models` 名单记录（发行层更新后不会复活）。`default` 与 `curation_default` 由声明它的最高层独占。

配置文件还有 `commitment` / `compression` / `checkpointer` / `database` 段，`extensions_config.json` 负责 skills / mcpServers；这些同样以用户偏好层优先。

**启动后端**

```powershell
cd backend/packages/harness
python -m uvicorn backend.app.gateway.app:app --host 127.0.0.1 --port 8765
```

它会应用 Alembic 迁移并连接 PostgreSQL。全新数据库先拉起：`docker compose -f desktop/compose.yaml up -d`。

**启动桌面壳**

```powershell
cd desktop
npm install && npm start        # electron .
```

---

## 目录结构

```
backend/
  app/desktop/                context · patrol · memory · compression 服务
    agent_loop/               委托权力 · Patrol · Kernel · coordinator
    context_evolution/        不可变 revision · DAG reader/publisher
    context_curation/         多 Context Program · Lane · Portfolio 原子发布
    run_orchestration/        唯一 PreparedRun → run_agent 执行脊柱
    workspace_coordination/   slot · lease · fencing · worktree · adoption
  app/gateway/                统一运行接口 (SSE/会话/鉴权)
  packages/harness/focus/
    agents/                   lead 装配 · 承诺层(九阶段) · 压缩门
    plugins/                  接口目录 · 注册表 · 桥接 · 远程
    runtime/                  run(worker) · checkpointer · stream-bridge · store
    tools/                    内置工具(权限门控) · 自定义工具注册表
    config/ · models/ · skills/ · mcp/ · persistence/
desktop/                      Electron 壳(零依赖 vanilla JS)
plugins/                      spatial-patrol 
```

---
