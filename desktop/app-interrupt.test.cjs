const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

let lastHtml = "";
let statusText = "";
let statusDanger = false;
const statusNode = {
  set textContent(value) { statusText = value; },
  get textContent() { return statusText; },
  classList: { toggle(cls, on) { if (cls === "danger") statusDanger = on; } },
};
const conversation = {
  clientHeight: 100,
  scrollHeight: 1000,
  scrollTop: 0,
  isConnected: false,
  prepend() {}, insertBefore() {}, append() {},
};
const inert = {
  addEventListener() {},
  classList: { toggle() {} },
  content: { cloneNode() {} },
  set textContent(value) {}, get textContent() { return ""; },
};
const app = { dataset: {} };
Object.defineProperty(app, "innerHTML", {
  set(value) { lastHtml = value; },
  get() { return lastHtml; },
});
const document = {
  body: { dataset: {} },
  addEventListener() {},
  querySelector(selector) {
    if (selector === "#app") return app;
    if (selector === "#conversation") return conversation;
    if (selector === "#globalStatus") return statusNode;
    if (selector === "#mainInput") return { value: "测试消息", focus() {} };
    return inert;
  },
};

let fetchCalls = [];
const fetchMock = async (path, options = {}) => {
  fetchCalls.push({ path, method: options.method || "GET" });
  // 默认响应：run 仍在运行（中断请求已受理）
  return { ok: true, status: 200, json: async () => ({ run_id: "run-1", status: "running", kind: "main" }) };
};

const context = vm.createContext({
  Headers,
  EventSource: class {},
  FormData: class {},
  clearTimeout() {},
  console,
  document,
  fetch: fetchMock,
  location: { origin: "http://localhost", protocol: "http:" },
  setTimeout() {},
  window: {},
});
const source = fs.readFileSync(require.resolve("./app.js"), "utf8").replace(/bootstrap\(\);\s*$/, "");
new vm.Script(source).runInContext(context);

const task = { task_id: "task", workspace_path: "C:/workspace", workspace_name: "workspace" };

function renderTask(activeRun) {
  return new vm.Script(`
    state.tasks = [{ task_id: "task", workspace_path: "C:/workspace", workspace_name: "workspace" }];
    state.activeTaskId = "task";
    state.details.set("task", { messages: [], ui_state: {}, active_run: ${JSON.stringify(activeRun || null)} });
    state.commitment.taskId = null;
    renderFocus();
  `).runInContext(context);
}

function setGlobal(name, value) {
  context[name] = value;
}

// 1) 无活动运行 → 不渲染中断按钮
renderTask(null);
assert.equal(lastHtml.includes('data-action="interrupt-main-run"'), false, "无活动运行不渲染中断按钮");

// 2) running 运行 → 渲染中断按钮
renderTask({ run_id: "run-1", status: "running", kind: "main" });
assert.equal(lastHtml.includes('data-action="interrupt-main-run"'), true, "running 渲染中断按钮");

// 3) pending 运行 → 渲染中断按钮
renderTask({ run_id: "run-2", status: "pending", kind: "main" });
assert.equal(lastHtml.includes('data-action="interrupt-main-run"'), true, "pending 渲染中断按钮");

// 4) 终态运行 → 不渲染
for (const status of ["success", "error", "interrupted"]) {
  renderTask({ run_id: "run-3", status, kind: "main" });
  assert.equal(lastHtml.includes('data-action="interrupt-main-run"'), false, `${status} 不渲染中断按钮`);
}

(async () => {
  // 5) 点击中断 → 调用 cancel API 并提示（cancel 受理即置 interrupted → 判定为受理成功）
  renderTask({ run_id: "run-1", status: "running", kind: "main" });
  fetchCalls = [];
  await new vm.Script(`interruptMainRun()`).runInContext(context);
  assert.equal(fetchCalls.length, 1, "中断调用 cancel API 一次");
  assert.ok(fetchCalls[0].path.endsWith("/desktop/api/runs/run-1/cancel"), "cancel URL 正确");
  assert.match(statusText, /已请求中断/, "中断提示已展示");

  // 6) 竞态：run 自己先到 success 终态 → 提示运行已结束
  renderTask({ run_id: "run-1", status: "running", kind: "main" });
  fetchCalls = [];
  setGlobal("fetch", async (path) => {
    fetchCalls.push({ path });
    return { ok: true, status: 200, json: async () => ({ run_id: "run-1", status: "success", kind: "main" }) };
  });
  await new vm.Script(`interruptMainRun()`).runInContext(context);
  assert.equal(fetchCalls.length, 1);
  assert.match(statusText, /运行已结束/, "终态载荷提示运行已结束");
  assert.equal(statusDanger, true, "终态提示为错误样式");

  // 7) busy 防连点：中断进行中再点击不重复请求
  renderTask({ run_id: "run-1", status: "running", kind: "main" });
  fetchCalls = [];
  setGlobal("fetch", async (path) => {
    fetchCalls.push({ path });
    return { ok: true, status: 200, json: async () => ({ run_id: "run-1", status: "running", kind: "main" }) };
  });
  await new vm.Script(`state.mainInterrupting = true; interruptMainRun();`).runInContext(context);
  assert.equal(fetchCalls.length, 0, "busy 期间不重复请求");

  // 8) 发送消息后 active_run 立即更新 → 运行中即渲染中断按钮
  renderTask(null);
  fetchCalls = [];
  setGlobal("fetch", async (path) => {
    fetchCalls.push({ path });
    return { ok: true, status: 200, json: async () => ({ run_id: "run-9", status: "pending", kind: "main", task_id: "task" }) };
  });
  await new vm.Script(`sendMain()`).runInContext(context);
  assert.ok(fetchCalls.length >= 2, "发送创建主 run 并保存 ui-state");
  assert.ok(fetchCalls[0].path.includes("/main/runs"), "首个请求创建主 run");
  const activeRunId = new vm.Script(`state.details.get("task").active_run.run_id`).runInContext(context);
  assert.equal(activeRunId, "run-9", "active_run 已更新");
  assert.equal(lastHtml.includes('data-action="interrupt-main-run"'), true, "运行中渲染中断按钮");

  console.log("app-interrupt.test.cjs OK");
})().catch(error => { console.error(error); process.exit(1); });
