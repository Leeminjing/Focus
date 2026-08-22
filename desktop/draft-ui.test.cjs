/*
 * 本文件验证 F20 小兵草稿工作台。输入为固定任务、草稿、模型和权限清单，输出为四段顺序结构、
 * 固定投放栏、自动保存/token 状态与原投放协议断言；工作流不访问真实 API 或 Electron。
 */
"use strict";

const assert = require("node:assert/strict");
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

assert.match(source, /\/desktop\/api\/drafts\/\$\{draft\.draft_id\}/, "草稿继续使用原 PUT API");
assert.match(source, /draft\.deployment_id \|\|= crypto\.randomUUID\(\)/, "投放继续使用稳定 deployment id 防重复");
assert.match(source, /state\.drafts\.set\(taskId, \{ \.\.\.saved/, "自动保存结果仍按捕获的任务隔离");
assert.match(source, /draftSaveRevisions\.get\(taskId\) === revision/, "乱序保存响应不得覆盖新草稿");
assert.match(source, /if \(!await saveDraft\(state\.activeTaskId\)\)/, "保存失败时投放必须停止");

console.log("draft UI: 四段工作台、自动保存、token、权限与投放协议通过");
