/*
 * 本文件验证主运行中断与必看报告恢复 UI。输入为可控运行状态、SSE/HTTP 载荷和桌面 DOM；
 * 输出为中断按钮状态、取消竞态、必看报告原因及 retry/cancel 恢复请求断言。
 * 具体工作流在 VM 中执行 app.js 并记录同源 API 调用；桌面 DOM 桩把会话容器内容并回整页 HTML，
 * 使整页断言在"会话区由对账写入"之后仍然成立，并复用宿主挂载节点桩支持任务检查器。示例：node desktop/src/renderer/app/app-interrupt.test.cjs。
 */
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { createAppHarness, createNode, readAppSource } = require("../../../tests/helpers/test-helper.cjs");

let lastHtml = "";
const app = createNode();
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
  set innerHTML(value) { lastHtml += value; },
  get innerHTML() { return ""; },
};
let interruptButton = createNode();

const harness = createAppHarness({
  statusNode: "record",
  fetch: true,
  selectors: {
    "#app": app,
    "#conversation": conversation,
    "#mainInput": { value: "测试消息", focus() {} },
    '[data-action="interrupt-main-run"]': () => interruptButton,
  },
});
const { context, statusNode, fetches } = harness;
new vm.Script(readAppSource()).runInContext(context);

const task = { task_id: "task", workspace_path: "C:/workspace", workspace_name: "workspace" };

function renderTask(activeRun) {
  return new vm.Script(`
    state.view = "focus";
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

  // 请求失败必须恢复当前按钮；期间的重渲染也不能令新按钮永久禁用。
  for (const rerender of [false, true]) {
    renderTask({ run_id: "run-retry", status: "running", kind: "main" });
    new vm.Script(`state.mainInterrupting = false`).runInContext(context);
    interruptButton = createNode();
    fetches.length = 0;
    let rejectRequest;
    setGlobal("fetch", (url) => {
      fetches.push({ url });
      return new Promise((resolve, reject) => { rejectRequest = reject; });
    });
    const pending = new vm.Script(`interruptMainRun()`).runInContext(context);
    assert.equal(interruptButton.disabled, true, "请求期间禁用按钮");
    await new vm.Script(`interruptMainRun()`).runInContext(context);
    assert.equal(fetches.length, 1, "实际请求期间不能重复提交");
    if (rerender) interruptButton = createNode({ disabled: true });
    rejectRequest(new Error("fixture cancellation failed"));
    await pending;
    assert.match(statusNode.textContent, /fixture cancellation failed/);
    assert.equal(interruptButton.disabled, false, "取消失败后无需切换会话即可重试");
    assert.equal(new vm.Script(`state.mainInterrupting`).runInContext(context), false);
    setGlobal("fetch", async (url) => {
      fetches.push({ url });
      return { ok: true, status: 200, json: async () => ({ status: "interrupted" }) };
    });
    await new vm.Script(`interruptMainRun()`).runInContext(context);
    assert.equal(fetches.length, 2, "第二次点击重新发出取消请求");
    assert.ok(fetches.every(call => call.url.endsWith("/runs/run-retry/cancel")));
  }

  // 请求结束时以当前运行状态为准，包括终态、切换会话及按钮已卸载。
  for (const nextRun of [null, { run_id: "run-ended", status: "success" }, { run_id: "run-other", status: "running" }]) {
    renderTask({ run_id: "run-before-switch", status: "running", kind: "main" });
    interruptButton = createNode();
    let rejectRequest;
    setGlobal("fetch", () => new Promise((resolve, reject) => { rejectRequest = reject; }));
    const pending = new vm.Script(`interruptMainRun()`).runInContext(context);
    context.nextRun = nextRun;
    new vm.Script(`
      state.activeTaskId = "other-task";
      state.details.set("other-task", { active_run: nextRun });
    `).runInContext(context);
    interruptButton = nextRun ? createNode({ disabled: true }) : null;
    rejectRequest(new Error("late cancellation failure"));
    await pending;
    if (interruptButton) assert.equal(interruptButton.disabled, nextRun.status !== "running");
    assert.equal(new vm.Script(`state.mainInterrupting`).runInContext(context), false);
  }

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

  new vm.Script(`
    state.tasks = [{ task_id: "task", thread_id: "thread", workspace_path: "C:/workspace", workspace_name: "workspace" }];
    state.details.set("task", {
      messages: [], ui_state: {}, active_run: { run_id: "run-mv", status: "interrupted", kind: "main" },
      pending_must_view_report: {
        type: "must_view_report",
        materials: [{ material_id: "m1", relative_path: "shot.png", reason: "unread" }],
      },
    });
    renderFocus();
  `).runInContext(context);
  assert.match(lastHtml, /必看图片报告/);
  assert.match(lastHtml, /shot\.png/);
  assert.match(lastHtml, /模型声明未读到/);
  assert.match(lastHtml, /data-action="retry-must-view"/);
  assert.match(lastHtml, /data-action="cancel-must-view"/);

  fetches.length = 0;
  setGlobal("fetch", async (path, options) => {
    fetches.push({ url: path, options });
    return { ok: true, status: 200, json: async () => ({ run_id: "run-mv-retry", status: "pending", kind: "main", task_id: "task" }) };
  });
  await new vm.Script(`resumeMustView("retry")`).runInContext(context);
  assert.ok(fetches[0].url.endsWith("/desktop/api/threads/thread/runs/resume"));
  assert.deepEqual(JSON.parse(fetches[0].options.body).resume, {
    type: "must_view_report", decision: "retry",
  });
  assert.equal(
    new vm.Script(`state.details.get("task").pending_must_view_report`).runInContext(context),
    null,
  );

  console.log("app-interrupt.test.cjs OK");
})().catch(error => { console.error(error); process.exit(1); });
