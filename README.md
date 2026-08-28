# Focus

**human in the contexts · 上下文手术**

> An open-source, local-first agent built on LangGraph. Its single conviction is the *in* of "human in the loop" — raised from the execution loop to the context itself: **the human participates in the contexts, and operates on them.**
>
> 一个基于 LangGraph 的开源、本地优先 Agent。它的唯一信念，是 "human in the loop" 里那个 *in*——从执行循环抬升到上下文本体：**人参与在 contexts 里，并直接在它们上面做手术。**

---

## The "in" is participation, not a location / 这个 "in" 是参与，不是坐标

"Human in the loop" (HITL) never meant a person is physically *inside* a loop. It names a state: inside an automated process, the human **participates as a decision-maker**, stepping in where the machine should not decide alone. That "in" is **participation**, not a spatial preposition.

"Human in the contexts" uses the same "in", one level higher.

- **HITL / in the loop** — the human participates at the **execution** level: tool calls, output stream, which actions are approved.
- **Focus / in the contexts** — the human participates at the **context** level: the substrate the model reasons over. The human decides which context becomes a contract, which messages get folded, which context gets forked, what a parallel agent wakes up with, and which memories survive across sessions.

"上下文手术" is the same sentence said with the hand — that human, holding the scalpel.

> **人参与在 contexts 里，并在它们上面做手术。**
> **The human participates in the contexts, and operates on them.**

---

## The core rule / 公理的内核

One rule reproduces itself across the system:

> **At every context-transformation boundary, box the model's uncertainty into a narrow, mechanically-checkable shell; then let deterministic code authorize and isolate, and the human approve and write. Messages are the single source of truth, checkpoint namespaces are identity, tombstones carry the source, and where the code cannot be sure, it hands back to the human rather than guessing.**

---

## Architecture / 架构

A single FastAPI gateway drives Focus over one `POST /api/threads/{thread_id}/runs/stream` SSE channel; the desktop shell loads it same-origin, no CORS.

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
                     │   (the one assembly seam)      │
                     │   middleware + tools + prompt  │
                     └──────────────┬───────────────┘
                                    │  run_agent (the one execution spine)
   ┌───────────────┬───────────┬────┴────────┬────────────────┐
   ▼               ▼           ▼             ▼                ▼
  commitment      compression  context fork  patrol/spatial   memory
  9-stage         gate         DAG (f15)     frozen snapshot   <memory>
```

- **Storage**: PostgreSQL + asyncpg checkpointer (survives restart) + LangGraph Store (`async_postgres`, vectors off).
- **Streaming**: in-process `StreamBridge` → SSE; `RunManager` tracks run status; checkpointer provides rollback snapshots.
- **Identity**: every agent is `(thread_id, checkpoint namespace)` — `patrol:{id}`, `{thread_id}:commitment` — the same engine in different rooms, so forking, isolation, and recovery are just namespaces.
- **Frontend**: zero-dependency static UI served by the same Python process (hand-written vanilla JS + design-token CSS; one vendored markdown lib). No npm UI deps, no build step, no framework.

---

## Core mechanisms / 核心机制

The context operations are implemented as five mechanisms. Each one is where the human participates, and where the human's decision is what gets kept.

### Compression mechanism / 压缩机制

The main agent estimates token usage before every model call. When usage crosses `context_window × threshold_ratio`, the graph interrupts and asks you — it never trims on its own.

- You select one or more message ranges (they may even split a tool-call group; the code repairs the protocol afterward).
- For each range the system generates a candidate summary; you accept, edit, regenerate, or cancel. What is kept is *your* text.
- You can also delete a selected range outright (a tombstone) instead of summarizing it.
- On confirm, messages are rewritten: each range becomes a compression block (a `HumanMessage` carrying the original source + a `block_id`, persisted with the checkpoint); a deletion becomes an empty tombstone.
- `wrap_model_call` strips the compression metadata before every model call, so the original source never reaches the model.
- Blocks can be expanded, re-edited, re-compressed, or undone; recovery goes through the resume channel. A pending compression request blocks a new main run (409) until resolved.

### Derived-context mechanism / 派生 contexts 机制

A context is not a single window; you can **fork** one. From one or more committed checkpoints of the same workspace, you derive a new context.

- The system keeps two things: `authored_messages` (what you wrote) and `execution_messages` (the valid projection the model runs on).
- The projector only **adds structure** — it repairs dangling tool calls with synthetic placeholders. Any change that would rewrite your text becomes `approval_required`, an explicit hash-bound `accept`/`reject`, never a silent rewrite.
- Each derived context gets a **fresh** `thread_id`; the projection is written into the new checkpoint, never mutating the parent.
- Contexts form a tree with depth and lineage you can trace, merge, branch, archive, and delete (a tombstone when it still has children).

### Patrol mechanism / patrol 机制

A patrol agent is a snapshot of your session forked into a **separate room**.

- From the main agent's latest committed checkpoint you deep-copy a frozen draft, then edit its `system_prompt`, history, and final message freely.
- Deployment is **idempotent** (`deployment_id` + unique constraint): repeat clicks never create a duplicate.
- It runs in its own namespace (`patrol:{id}`), in parallel with your main line and never interrupting it.
- Its results are **never auto-injected** into your main context. You read them when you choose, via `list_patrol_agents` / `read_patrol_agent_history`.
- It has an independent lifecycle — cancel its run, retry on the original frozen input, or append a new message to continue.
- **Spatial patrol** pins the same idea to a place: drop a patrol agent onto a coordinate in a document or page, and it observes outward from that point; position is its identity. DOCX edits require explicit read-or-write authorization.

### Commitment layer / 承诺层

Before the work begins, Focus makes you **align on a contract** — and the alignment machinery is deliberately not an LLM.

- `/commit <指令>` triggers a nine-phase state machine (goal / requirements & conflicts / priorities / inputs / versions / official knowledge / contract / task_contract / final message).
- The supervisor is a deterministic `StateGraph` that fabricates an `AIMessage` (a synthetic `tool_call` to `delegate_with_review`) and routes it through a `ToolNode`. The LLM only runs inside the tool body (Worker + Evaluator); which stage, what's next, when to stop — that is pure code. Stage order is locked: `expected = stage + 1; stage != expected → invalid_stage`.
- A Worker proposes; an independent Evaluator reviews; a deterministic validator checks structure. After three failed reviews it hands the decision to you.
- You approve or revise stages 3/5/6/7 (and conditionally 2/4) by hand.
- When signed, the contract **replaces the `/commit` message in place** (same message id), in the isolated namespace `{thread_id}:commitment`; the pre-commit conversation is untouched.
- Because the supervisor is code, it cannot be prompt-injected to skip or reorder a stage; the LLM's reach is confined to a schema-gated content box.

### Memory library / 记忆库

Memory is not the model's job here; it is **yours** to curate. Nothing is auto-captured or auto-recalled.

- You pull from a whole session, particular (non-contiguous) messages, a span of text, or your own words — any combination.
- You compress it into a `complete` overview or independent `segmented` notes, then **edit the draft yourself**; what is saved is *your* version, not the model's.
- Each memory carries its `source_snapshot` and per-segment `source_ref`, so it is always attributable and re-generatable.
- Before a new session you choose which memories to carry in, injected as a `<memory>` block into the system prompt — to the main agent only.

---

## Context operations: the division of labor / Context 操作：人与代码的分工

Each governed context operation collapses the model's freedom into a narrow artifact. Code owns the **authorization and isolation** of that artifact; the LLM owns the **content**; the human owns the **decision**.

| Operation | Context artifact | Code owns | LLM owns | Human owns |
|---|---|---|---|---|
| **compression** | compressed / tombstoned messages | validate ranges, repair protocol, strip metadata, carry source | propose summary | pick range · write or rewrite · delete · undo |
| **derived context** | authored / execution projection | compile projection (add-only), hash-bound accept/reject, fresh thread_id, lineage | — (human-authored) | author messages · accept or reject |
| **patrol** | patrol / spatial context | idempotent deploy, namespace isolation, no auto-inject, reader tools | do the work | rewrite draft · deploy · choose to read |
| **commitment** | task contract | supervisor (code), validator, in-place replace, namespace | propose stage content | approve / revise each stage |
| **memory** | memory (complete / segmented) + `<memory>` block | resolve source, slice text, render block, `_safe_attr` | propose the compressed draft | pick source · edit draft · choose what to carry in |

---

## Capabilities / 能力地图

- **承诺层 (Commitment)**: `/commit <指令>` triggers 9 phases; Worker–Evaluator review; fixed human pauses at phases 3/5/6/7; outputs a `task-contract`. Context7 is loaded lazily for this flow only.
- **压缩机制 (Compression)**: interrupt-on-threshold; user picks ranges; LLM summary draft; tombstone delete / undo / reopen; source retained with the checkpoint, never seen by the model.
- **派生 contexts 机制 (Derived context forking)**: derive from single/multiple parent checkpoints; `authored vs execution` separation; hash-bound accept/reject; lineage/depth/tree; fresh thread_id.
- **patrol 机制 (Patrol)**: frozen draft + full context rewrite + idempotent deploy + isolated namespace + results not auto-injected.
- **空间小兵 (Spatial)**: coordinate-anchored deploy, observe outward, position is identity, DOCX explicit read/write authorization.
- **记忆库 (Memory)**: multi-source (session / messages / text / manual), complete / segmented, human-edit-then-save, `<memory>` injection into a new session (main only), cross-session persistence.
- **插件 (Plugins)**: interface catalog (tool/hook/service) + dependency-injection registry + a single bridge middleware + a language-neutral stdio remote protocol; can carry desktop API routes and frontend assets.
- **技能 (Skills)**: `skills/public` + per-user `custom`; enabled via `extensions_config.json`; `describe_skill` discovery.
- **工具错误收口 (Tool errors)**: catches tool exceptions and returns a recoverable `ToolMessage` so the loop continues.

---

## Tech stack / 技术栈

- **Language/framework**: Python · LangChain · LangGraph (`create_agent`).
- **Service**: FastAPI (gateway) · in-process `StreamBridge` · SSE.
- **Storage**: PostgreSQL (asyncpg) · Alembic · LangGraph Store (`async_postgres`, vectors off).
- **Models**: OpenAI-compatible (default `deepseek-v4-flash-vision-exp` via `focus.models.deepseek`), pluggable via `use: module:Class`.
- **External**: MCP (Context7 docs, Playwright browser); vendored vanilla-JS frontend.
- **Config**: `config.yaml` · `extensions_config.json` · `plugins/<name>/plugin.json`.

---

## Quick start / 快速开始

**Requirements** — Python ≥ 3.11 (with `alembic` and `uvicorn`), `git`, `docker` (for the bundled PostgreSQL via `desktop/compose.yaml`), Node.js (for the Electron shell), and an OpenAI-compatible key (DeepSeek) in `OPENAI_API_KEY`.

**Configure** — `cp .env.example .env` and set `OPENAI_API_KEY` (optional `VISION_API_KEY`, `FOCUS_DATABASE_URL`). Edit `config.yaml` (models / commitment / compression / checkpointer / database) and `extensions_config.json` (skills / mcpServers). `~/.focus/` is a global default layer, overlaid by the repo config.

**Start the backend**

```powershell
cd backend/packages/harness
python -m uvicorn backend.app.gateway.app:app --host 127.0.0.1 --port 8765
```

It applies Alembic migrations and connects to PostgreSQL. For a fresh database: `docker compose -f desktop/compose.yaml up -d`.

**Start the desktop shell**

```powershell
cd desktop
npm install && npm start        # electron .
```

---

## Repo layout / 目录结构

```
backend/
  app/desktop/                context · patrol · memory · compression services
  app/gateway/                unified run interface (SSE / session / auth)
  packages/harness/focus/
    agents/                   lead assembly · commitment (9-stage) · compression gate
    plugins/                  interface catalog · registry · bridge · remote
    runtime/                  runs (worker) · checkpointer · stream-bridge · store
    tools/                    builtin tools (permission-gated) · custom-tool registry
    config/ · models/ · skills/ · mcp/ · persistence/
desktop/                      Electron shell (zero-dependency vanilla JS)
plugins/                      spatial-patrol 
```

---
