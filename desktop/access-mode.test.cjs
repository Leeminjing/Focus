/*
 * 本文件验证「本机资源访问模式」这一概念的唯一归属地：默认最严、写入规则、放宽判据、请求字段，
 * 以及作曲区与草稿装备两个入口都从同一处渲染（含策展小兵的钉定形态）。
 *
 * 输入为持有模式的对象、模式取值与渲染结果；输出为归一结论、请求字段与渲染出的 HTML。
 * 工作流：先验纯逻辑，再验作曲区与草稿装备的选择器渲染，最后验主运行请求确实携带该模式。
 */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

const mode = require("./access-mode.js");

// 1) 只有一个默认值：缺失或无法识别都按最严处理，绝不臆测为完全权限
assert.equal(mode.DEFAULT_MODE, "workspace");
assert.deepEqual([...mode.MODES], ["workspace", "full"]);
assert.equal(mode.normalize(undefined), "workspace");
assert.equal(mode.normalize("bogus"), "workspace");
assert.equal(mode.normalize("full"), "full");
assert.equal(mode.readMode(undefined), "workspace");
assert.equal(mode.readMode({}), "workspace");
assert.equal(mode.readMode({ access_mode: "bogus" }), "workspace");
assert.equal(mode.readMode({ access_mode: "full" }), "full");

// 2) 只有一条写入规则：不改原对象，返回新的持有对象
const holder = { input: "你好", access_mode: "workspace" };
const widened = mode.writeMode(holder, "full");
assert.deepEqual(widened, { input: "你好", access_mode: "full" });
assert.equal(holder.access_mode, "workspace", "原对象不被改写");
assert.equal(mode.writeMode(holder, "bogus").access_mode, "workspace", "非法取值按最严写入");

// 3) 只有一处放宽判据：只有 workspace → full 需要确认
assert.equal(mode.widens("workspace", "full"), true);
assert.equal(mode.widens("full", "workspace"), false, "收窄不需要确认");
assert.equal(mode.widens("full", "full"), false);
assert.equal(mode.widens(undefined, "full"), true, "未知取值按最严读，因此仍视为放宽");

// 4) 请求字段与展示文案
assert.deepEqual(mode.requestBody("full"), { access_mode: "full" });
assert.deepEqual(mode.requestBody(undefined), { access_mode: "workspace" });
assert.equal(mode.labelKey("full"), "access.mode_full");
assert.equal(mode.labelKey("bogus"), "access.mode_workspace");

// 5) 风险说明只有一份：两点文案 + 由调用方命名的两个动作
const translated = [];
const t = (key, fallback) => { translated.push(key); return fallback || key; };
const notice = mode.riskNoticeHtml(t, { confirm: "confirm-access-mode", cancel: "cancel-access-mode" });
assert.deepEqual([...mode.RISK_NOTICE_KEYS], ["access.risk_os_permissions", "access.risk_other_reviews"]);
for (const key of ["access.risk_title", ...mode.RISK_NOTICE_KEYS]) {
  assert.ok(translated.includes(key), `风险说明必须渲染 ${key}`);
}
assert.match(notice, /data-action="confirm-access-mode"/);
assert.match(notice, /data-action="cancel-access-mode"/);
const panelNotice = mode.riskNoticeHtml(t, {
  confirm: "confirm-access-review-full",
  cancel: "cancel-access-mode",
});
assert.match(panelNotice, /data-action="confirm-access-review-full"/, "批准面板用自己的确认动作");
assert.equal(mode.riskNoticeHtml(t).includes("data-action"), false, "未给出动作名时不渲染按钮");

// 6) 两个入口共用同一渲染：作曲区与草稿装备
const app = { dataset: {}, innerHTML: "" };
Object.defineProperty(app, "innerHTML", {
  set(value) { app.html = value; },
  get() { return app.html || ""; },
});
const conversation = {
  clientHeight: 100, scrollHeight: 1000, scrollTop: 0, isConnected: false,
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
harness.context.fetch = async (url, options = {}) => {
  harness.fetches.push({ url, options });
  return { ok: true, status: 200, json: async () => ({ run_id: "run-mode", status: "pending", task_id: "task" }) };
};
new vm.Script(readAppSource()).runInContext(harness.context);

const seed = uiState => new vm.Script(`
  state.tasks = [{ task_id: "task", thread_id: "thread", workspace_id: "ws", workspace_path: "C:/workspace" }];
  state.activeTaskId = "task";
  state.details.set("task", { messages: [], ui_state: ${JSON.stringify(uiState)}, active_run: null });
  state.equipment = { models: [], tools: [], skills: [], permissions: ["read", "write", "host_command"] };
  renderFocus();
`).runInContext(harness.context);

seed({});
assert.ok(app.innerHTML.includes('class="access-mode-chip is-workspace"'), "默认渲染工作区保护胶囊");
assert.ok(app.innerHTML.includes("工作区保护"), "胶囊显示当前模式");
assert.ok(app.innerHTML.includes('data-action="toggle-access-mode"'), "胶囊可展开");
assert.ok(app.innerHTML.includes('data-access-mode-value="workspace"'), "菜单含工作区保护");
assert.ok(app.innerHTML.includes('data-access-mode-value="full"'), "菜单含本机完全权限");
assert.ok(app.innerHTML.includes("操作系统自身的权限"), "菜单内含风险说明的第一点");
assert.ok(app.innerHTML.includes("其他人工审核机制"), "菜单内含风险说明的第二点");

seed({ access_mode: "full" });
assert.ok(app.innerHTML.includes('class="access-mode-chip is-full"'), "持久化为完全权限后胶囊随之切换");
assert.ok(app.innerHTML.includes("本机完全权限"), "胶囊显示完全权限");

// 6a) 草稿装备：普通小兵可选，策展小兵钉定且不给可点选择
const draftHtml = draft => new vm.Script(`
  state.drafts.set("task", ${JSON.stringify(draft)});
  renderEquipment(state.drafts.get("task"));
`).runInContext(harness.context);
const selectable = draftHtml({ mode: "standard", equipment: { permissions: ["read"], access_mode: "workspace" } });
assert.ok(selectable.includes('data-action="toggle-access-mode"'), "普通小兵草稿提供模式选择");
const pinned = draftHtml({ mode: "context_curator", equipment: { permissions: ["read"] } });
assert.ok(pinned.includes("is-pinned"), "策展小兵草稿显示钉定形态");
assert.equal(pinned.includes('data-action="toggle-access-mode"'), false, "策展小兵不提供可点选择");
assert.ok(pinned.includes("工作区保护"), "策展小兵如实显示工作区保护");

// 7) 主运行请求确实携带该模式（默认最严；持久化为完全权限后跟随）
(async () => {
  for (const [uiState, expected] of [[{}, "workspace"], [{ access_mode: "full" }, "full"]]) {
    seed(uiState);
    harness.fetches.length = 0;
    await new vm.Script("sendMain()").runInContext(harness.context);
    const create = harness.fetches.find(item => item.url.includes("/main/runs"));
    assert.ok(create, "发送应创建主运行");
    assert.equal(JSON.parse(create.options.body).access_mode, expected);
  }
  console.log("access-mode.test.cjs OK");
})().catch(error => { console.error(error); process.exit(1); });
