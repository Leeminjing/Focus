---
schema: focus.strategy/v1
id: focus/split
name: 任务拆分
description: 职责独立且依赖明确的子任务。
version: 1.0.0
---

## applicability

职责独立且依赖明确的子任务。

## evolution

按职责组织 Context，有依赖时等待真实前置结果，满足隔离才并行。

## inheritance

各自任务、接口、约束及已确认前置结果。
