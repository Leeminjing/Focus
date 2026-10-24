/* 恢复事件执行真实 app 对账路径，不重挂输入，不自动重放尚未确认的投递。 */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { createAppHarness, createNode, readAppSource } = require("../../../tests/helpers/test-helper.cjs");
const { createElement } = require("../../../tests/helpers/test-dom.cjs");

for (const pending of [false, true]) test(`服务恢复保留草稿和阅读位置，不生成新请求（已有未确认身份：${pending}）`, async () => {
  const input = createNode({ value: "服务断开时继续写的新草稿", selectionStart: 2, selectionEnd: 5 });
  const conversation = createElement("div");
  Object.assign(conversation, { scrollTop: 200, scrollHeight: 2000, clientHeight: 400 });
  const root = createNode({ querySelector: () => null });
  Object.defineProperty(root, "innerHTML", { set() { throw new Error("恢复不能重建工作区"); } });
  const h = createAppHarness({ fetch: true, statusNode: "record", selectors: {
    "#mainInput": input, "#conversation": conversation, "#app": root,
  } });
  const evaluate = code => vm.runInContext(code, h.context);
  evaluate(readAppSource());
  evaluate(`
    state.view = 'focus'; state.activeTaskId = 'task';
    state.tasks = [{task_id:'task', workspace_id:'workspace'}];
    state.details.set('task', {messages:[], ui_state:{input:'旧文本'}});
    state.contextTrees.set('workspace', []);
    composerDraft.claim('task', '服务断开时继续写的新草稿');
  `);
  if (pending) evaluate(`
    taskRunOperations.freeze('task', 'original-request', {body:'{"message":"原请求"}', input:'原请求', selectionSignature:'{"bindings":[],"requiredImageIds":[],"notes":{}}'});
    taskRunOperations.editRecovery('task');
  `);
  const requests = [];
  h.context.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    const route = new URL(url).pathname;
    let payload = [];
    if (route === "/desktop/api/tasks") payload = [{ task_id: "task", workspace_id: "workspace" }];
    else if (route === "/desktop/api/tasks/task") payload = {
      messages: [{ id: "from-server", role: "ai", content: "恢复后的权威消息" }], ui_state: { input: "后端旧草稿" },
    };
    else if (route.endsWith("/skills")) payload = { skills: [] };
    return { ok: true, status: 200, json: async () => payload };
  };
  await h.listeners.get("focus:gateway-restored")[0]();
  assert.equal(h.statusState.danger, false, h.statusState.text);
  assert.match(conversation.textContent, /恢复后的权威消息/);
  assert.equal(conversation.scrollTop, 200);
  assert.equal(input.value, "服务断开时继续写的新草稿");
  assert.equal(input.selectionStart, 2);
  assert.equal(input.selectionEnd, 5);
  assert.equal(evaluate("composerDraft.value('task')"), input.value);
  assert.equal(requests.some(request => request.url.endsWith("/main/runs")), false);
  assert.ok(requests.some(request => request.options.method === "PUT" && JSON.parse(request.options.body).input === input.value));
  if (pending) {
    assert.equal(evaluate("taskRunOperations.recovery('task').request_id"), "original-request");
    assert.equal(evaluate("JSON.parse(taskRunOperations.recovery('task').body).message"), "原请求");
  }
});
