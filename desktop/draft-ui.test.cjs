/*
 * 本文件验证 F20 小兵草稿工作台。输入为固定任务、草稿、模型和权限清单，输出为四段顺序结构、
 * 固定投放栏、自动保存/token 状态与原投放协议断言；工作流不访问真实 API 或 Electron。
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

let html = "";
const app = { dataset: {} };
Object.defineProperty(app, "innerHTML", { get: () => html, set: value => { html = value; } });

const { context } = createAppHarness({ selectors: { "#app": app } });
const source = readAppSource();
new vm.Script(source).runInContext(context);

new vm.Script(`
  state.tasks = [{ task_id: "task", title: "复杂任务", workspace_name: "workspace" }];
  state.activeTaskId = "task";
  state.equipment = {
    models: [{ name: "model", display_name: "Model", context_window: 32000 }],
    tools: [], skills: [], permissions: ["read", "write", "host_command"]
  };
  state.drafts.set("task", {
    task_id: "task", draft_id: "draft", source_checkpoint_id: "checkpoint-full-value",
    system_prompt: "system", history_messages: [{ role: "human", content: "history" }],
    final_human_message: "finish", token_estimate: 1200,
    equipment: { model_name: "model", skills: [], permissions: ["read"] }
  });
  renderDraft();
`).runInContext(context);

for (const heading of ["目标摘要", "消息编排", "装备与权限", "确认投放"]) {
  assert.match(html, new RegExp(heading), `草稿包含 ${heading}`);
}
assert.equal((html.match(/data-step="/g) || []).length, 4, "草稿严格呈现四段工作流");
assert.match(html, /data-draft-field="system_prompt"/, "System Prompt 仍可编辑");
assert.match(html, /data-draft-field="final_human_message"/, "最终 HumanMessage 仍可编辑");
assert.match(html, /data-message-field="content"/, "历史消息编排仍可编辑");
assert.match(html, /data-permission="host_command"/, "宿主命令权限仍显式呈现");
assert.match(html, /id="draftSaveState"[^>]*>自动保存/, "固定操作区呈现自动保存状态");
assert.match(html, /id="tokenCount">估算 1200 tokens/, "固定操作区呈现 token 估算");
assert.match(html, /data-action="exit-draft"/, "保留退出并保存动作");
assert.match(html, /data-action="deploy"/, "保留确认投放动作");
assert.match(html, /aria-label="Patrol 运行模式"/, "会话与全图共用的草稿呈现 Patrol 模式选择");
assert.match(html, /data-mode="context_curator">Context 策展/, "共享草稿可切换 Context 策展模式");

new vm.Script(`
  const curatorDraft = state.drafts.get("task");
  curatorDraft.mode = "context_curator";
  curatorDraft.default_curation_policy = {
    instructions: "持续保留核心上下文",
    preserve_rules: ["保留目标", "保留硬约束"],
    discard_rules: ["丢弃失败工具调用"],
  };
  curatorDraft.default_curation_model_name = "curator-model";
  curatorDraft.mode = "standard";
  setDraftPatrolMode(curatorDraft, "context_curator");
  renderDraft();
`).runInContext(context);
assert.match(html, /data-curation-policy="instructions"[^>]*>持续保留核心上下文</, "默认策展指令进入可编辑表单");
assert.match(html, /data-curation-policy="preserve_rules"[^>]*>保留目标\n保留硬约束</, "默认保留规则进入可编辑表单");
assert.match(html, /data-curation-policy="discard_rules"[^>]*>丢弃失败工具调用</, "默认丢弃规则进入可编辑表单");
assert.equal(
  new vm.Script(`state.drafts.get("task").equipment.model_name`).runInContext(context),
  "curator-model",
  "切换策展模式时使用显式策展默认模型",
);

const viewStyles = fs.readFileSync(require.resolve("./styles/views.css"), "utf8");
assert.match(
  viewStyles,
  /\.draft-panel\s*\{[\s\S]*?grid-template-rows:\s*auto auto minmax\(0,\s*1fr\) auto;/,
  "标题、模式选择、滚动正文与投放栏分别占据明确网格轨道",
);

assert.match(source, /\/desktop\/api\/drafts\/\$\{draft\.draft_id\}/, "草稿继续使用原 PUT API");
assert.match(source, /draft\.deployment_id \|\|= crypto\.randomUUID\(\)/, "投放继续使用稳定 deployment id 防重复");
assert.match(source, /state\.drafts\.set\(taskId, \{ \.\.\.saved/, "自动保存结果仍按捕获的任务隔离");
assert.match(source, /draftSaveRevisions\.get\(taskId\) === revision/, "乱序保存响应不得覆盖新草稿");
assert.match(source, /if \(!await saveDraft\(state\.activeTaskId\)\)/, "保存失败时投放必须停止");
assert.match(source, /\/context-curation\/quick-deploy/, "待命 Patrol 快捷策展走后端原子入口");

console.log("draft UI: 四段工作台、自动保存、token、权限与投放协议通过");
