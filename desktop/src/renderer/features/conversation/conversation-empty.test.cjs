"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const { createAppHarness, readAppSource } = require("../../../../tests/helpers/test-helper.cjs");

function setup() {
  const harness = createAppHarness();
  const run = source => harness.vm.runInContext(source, harness.context);
  run(readAppSource());
  run(`var emptyTask = { task_id: 'empty', harness_mode: 'workspace', workspace_path: 'C:/private' };`);
  return run;
}

test("empty conversation is an action surface, outside the message history", () => {
  const run = setup();
  const html = run("renderConversation({ messages: [] }, emptyTask)");
  assert.match(html, /data-unit-key="placeholder"/);
  assert.match(html, /data-action="focus-main-input"/);
  assert.doesNotMatch(html, /<article|work-record|message-role|READY|C:\/private/);
  assert.doesNotMatch(run("conversationMarkdown({ messages: [] }, emptyTask)"), /输入任务|从一个任务开始/);
  assert.doesNotMatch(run("renderConversation({ messages: [{ role: 'human', content: '开始' }] }, emptyTask)"), /conversation-empty/);
});

test("streamed content replaces the empty action surface even before history arrives", () => {
  const run = setup();
  run(`state.streamBuffers.set('run', { taskId: 'empty', text: '正在处理' });`);
  const html = run("renderConversation({ messages: [] }, emptyTask)");
  assert.match(html, /正在处理/);
  assert.doesNotMatch(html, /conversation-empty|focus-main-input/);
});

test("assembly empty state keeps its target scope separate from workspace tasks", () => {
  const run = setup();
  const render = scope => run(`renderConversation({ messages: [] }, { ...emptyTask, harness_mode: 'assembly', assembly_target: { scope: '${scope}' } })`);
  assert.match(render("global"), /全局装配可供所有工作区使用/);
  assert.match(render("workspace"), /当前装配仅对所选工作区生效/);
});
