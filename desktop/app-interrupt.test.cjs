const assert = require("node:assert/strict");
const vm = require("node:vm");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

let lastHtml = "";
const app = { dataset: {} };
Object.defineProperty(app, "innerHTML", {
  set(value) { lastHtml = value; },
  get() { return lastHtml; },
});
const conversation = {
  clientHeight: 100,
  scrollHeight: 1000,
  scrollTop: 0,
  isConnected: false,
  prepend() {}, insertBefore() {}, append() {},
};

const harness = createAppHarness({
  statusNode: "record",
  fetch: true,
  selectors: {
    "#app": app,
    "#conversation": conversation,
    "#mainInput": { value: "测试消息", focus() {} },
  },
});
const { context, statusNode, fetches } = harness;
new vm.Script(readAppSource()).runInContext(context);

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
  fetches.length = 0;
  await new vm.Script(`interruptMainRun()`).runInContext(context);
  assert.equal(fetches.length, 1, "中断调用 cancel API 一次");
  assert.ok(fetches[0].url.endsWith("/desktop/api/runs/run-1/cancel"), "cancel URL 正确");
  assert.match(statusNode.textContent, /已请求中断/, "中断提示已展示");

  // 6) 竞态：run 自己先到 success 终态 → 提示运行已结束
  renderTask({ run_id: "run-1", status: "running", kind: "main" });
  fetches.length = 0;
  setGlobal("fetch", async (path) => {
    fetches.push({ url: path });
    return { ok: true, status: 200, json: async () => ({ run_id: "run-1", status: "success", kind: "main" }) };
  });
  await new vm.Script(`interruptMainRun()`).runInContext(context);
  assert.equal(fetches.length, 1);
  assert.match(statusNode.textContent, /运行已结束/, "终态载荷提示运行已结束");
  assert.equal(harness.statusState.danger, true, "终态提示为错误样式");

  // 7) busy 防连点：中断进行中再点击不重复请求
  renderTask({ run_id: "run-1", status: "running", kind: "main" });
  fetches.length = 0;
  setGlobal("fetch", async (path) => {
    fetches.push({ url: path });
    return { ok: true, status: 200, json: async () => ({ run_id: "run-1", status: "running", kind: "main" }) };
  });
  await new vm.Script(`state.mainInterrupting = true; interruptMainRun();`).runInContext(context);
  assert.equal(fetches.length, 0, "busy 期间不重复请求");

  // 8) 发送消息后 active_run 立即更新 → 运行中即渲染中断按钮
  renderTask(null);
  fetches.length = 0;
  setGlobal("fetch", async (path) => {
    fetches.push({ url: path });
    return { ok: true, status: 200, json: async () => ({ run_id: "run-9", status: "pending", kind: "main", task_id: "task" }) };
  });
  await new vm.Script(`sendMain()`).runInContext(context);
  assert.ok(fetches.length >= 2, "发送创建主 run 并保存 ui-state");
  assert.ok(fetches[0].url.includes("/main/runs"), "首个请求创建主 run");
  const activeRunId = new vm.Script(`state.details.get("task").active_run.run_id`).runInContext(context);
  assert.equal(activeRunId, "run-9", "active_run 已更新");
  assert.equal(lastHtml.includes('data-action="interrupt-main-run"'), true, "运行中渲染中断按钮");

  console.log("app-interrupt.test.cjs OK");
})().catch(error => { console.error(error); process.exit(1); });
