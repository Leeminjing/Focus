# Focus

**human in the contexts · 上下文手术**

> 一个基于 LangGraph 的开源、本地优先 Agent。它的唯一信念，是 "human in the loop" 里那个 *in*——从执行循环抬升到上下文本体：**人参与在 contexts 里，并直接在它们上面做手术。**

> 经过筛选的精确的上下文优于臃肿但缓存命中率高的上下文，并且随着 run 的轮次增长，前者的花费反而可能低于后者。
>
> A carefully curated, precise context is better than a bloated context with a high cache hit rate, and as the number of run iterations increases, the former may actually become less expensive than the latter.

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

- **承诺层 (Commitment)**: `/commit <指令>` 触发 9 阶段；Worker–Evaluator 审核；阶段 3/5/6/7 固定人工暂停；输出 `task-contract`。Context7 仅为该流程懒加载。
- **压缩机制**: 触发阈值 + 用户划范围 + LLM 摘要草稿 + 墓碑删除/撤销/重开；来源随 checkpoint 保留，模型看不到。
- **派生 contexts 机制**: 从单/多父 checkpoint 派生；`authored vs execution` 分离；哈希绑定 accept/reject；lineage/depth/tree；全新 thread_id。
- **patrol 机制**: 用户的委托式上下文操作者 —— 注意力隔离（支线任务不占主线）+ 上下文操作（代表用户策展/派生/压缩/整理记忆）；实现为冻结草稿 + 全量重写上下文 + 幂等投放 + 独立命名空间 + 结果不自动注入。**快捷策展**为你派生一个更干净的 context。
- **记忆库 (Memory)**: 多来源（会话/消息/文字/手输）、complete/segmented、人工编辑后保存、`<memory>` 注入新会话（仅 main）、跨会话持久化。
- **插件 (Plugins)**: 接口目录（tool/hook/service）+ 依赖注入注册表 + 单一桥接中间件 + 语言中立 stdio 远程协议；可携带桌面 API 路由与前端资源。

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
| `FOCUS_MODEL` | 否 | 覆盖默认模型，取 `config.yaml` models 中已存在的条目名 |
| `FOCUS_DATABASE_URL` | 否 | 覆盖数据库连接 |

变量可写入 `.env`（`cp .env.example .env`），也可用操作系统环境变量提供（优先级更高）：Windows `setx OPENAI_API_KEY "sk-..."`，macOS/Linux `export OPENAI_API_KEY=...`；两者都需新开终端生效。

编辑 `config.yaml`（models / commitment / compression / checkpointer / database）与 `extensions_config.json`（skills / mcpServers）。`~/.focus/` 是全局默认层，由仓库态配置覆盖。

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
