/* 验证运行详情实际请求、完整链展示、失败重试和任务切换的迟到响应隔离。 */
"use strict";
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { createAppHarness, createNode, readAppSource } = require("../helpers/test-helper.cjs");

const requests = [];
const content = createNode({ insertAdjacentHTML(_position, html) { this.innerHTML += html; } });
const harness = createAppHarness({
  selectors: { "#inspectorContent": content, "#appInspector": createNode() },
  globals: { fetch: (url, options) => new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })) },
});
const context = harness.context;
new vm.Script(readAppSource()).runInContext(context);
const evaluate = code => new vm.Script(code).runInContext(context);
const flush = () => new Promise(resolve => setImmediate(resolve));
const chain = {
  directive: { directive_id: "directive-1", content: "检查 <script>bad()</script>", origin: "patrol", state: "settled", correlation_id: "correlation-1" },
  decision: { rationale: "测试失败，需要修复" },
  transitions: [{ state: "authorized", occurred_at: "2026-10-18T01:00:00Z" }, { state: "settled", run_id: "run-1", reason: "运行完成" }],
  runs: [{ run_id: "run-1", status: "success" }],
  workspace: [{ run_id: "run-1", slot_id: "slot-1", root_path: "C:/work/tree", kind: "isolated", observed_workspace_revision: 1, resulting_workspace_revision: 2, adoption_state: "pending", effect_evidence: [{ path: "result.txt" }] }],
  events: [{ sequence: 5, kind: "context.workspace.changed", entity_id: "run-1", payload: { path: "result.txt" } }],
};
function respond(request, causality) {
  request.resolve({ ok: true, status: 200, json: async () => ({ run_id: "run-1", causality }) });
}

(async () => {
  evaluate(`state.tasks = [{ task_id: "task-1", title: "Task", latest_run: { run_id: "run-1", status: "success" } }]; state.activeTaskId = "task-1"; state.view = "focus"; state.inspector.open = true; state.inspector.tab = "run"; renderInspector();`);
  assert.equal(requests.length, 1);
  assert.match(requests[0].url, /\/runs\/run-1\/causality$/);
  assert.equal(requests[0].options.headers.get("X-Focus-Session"), "focus-dev-session");
  assert.match(content.innerHTML, /正在读取指令因果链/);
  respond(requests[0], chain);
  await flush();
  for (const text of ["测试失败，需要修复", "已授权", "已结算", "C:/work/tree", "result.txt", "context.workspace.changed", "1 → 2"]) assert.ok(content.innerHTML.includes(text), text);
  assert.ok(content.innerHTML.includes("&lt;script&gt;"));
  assert.ok(!content.innerHTML.includes("<script>bad()"));
  evaluate("renderInspector()");
  assert.equal(requests.length, 1, "重绘不重复请求");

  evaluate('readRunCausality(activeTask().latest_run, true); renderInspector()');
  requests[1].reject(new Error("读取断开"));
  await flush();
  assert.match(content.innerHTML, /读取断开/);
  assert.match(content.innerHTML, /重试/);
  evaluate('readRunCausality(activeTask().latest_run, true); renderInspector()');
  respond(requests[2], null);
  await flush();
  assert.match(content.innerHTML, /未关联 Directive/);

  evaluate('readRunCausality(activeTask().latest_run, true); state.tasks.push({ task_id: "task-2", title: "Other", latest_run: { run_id: "run-2", status: "running" } }); state.activeTaskId = "task-2"; renderInspector()');
  respond(requests[4], null);
  await flush();
  respond(requests[3], chain);
  await flush();
  assert.ok(!content.innerHTML.includes("directive-1"), "旧任务的迟到响应不覆盖当前详情");
  evaluate('activeTask().latest_run.status = "success"; renderInspector()');
  assert.equal(requests.length, 6, "运行结算后重新读取因果链");
  respond(requests[5], chain);
  await flush();
  console.log("directive causality UI passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
