/* 验证真实 app.js 的设置 change 事件、重开显示及普通任务/Patrol 请求共用已保存默认模式。
 * Patrol store 在设置变更前创建，发送失败后切换默认值，重试必须保留原请求权限。
 * 示例：node --test desktop/new-session-access-mode.test.cjs。
 */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { createAppHarness, createNode, readAppSource } = require("./test-helper.cjs");

const storageKey = "focus-new-session-file-mode-v1";
async function setup() {
  const select = createNode({ id: "newSessionAccessMode", value: "workspace-write" });
  const settings = createNode({ querySelector: selector => selector === "#newSessionAccessMode" ? select : null });
  const patrolPosts = [];
  const harness = createAppHarness({ statusNode: "record", fetch: true,
    selectors: { "#settingsDialog": settings, "#modelSettingsHost": () => null },
    globals: {
      FocusSurfaceTransition: { visible: async (node, visible) => { node.open = visible; } },
      FocusLoopApi: { create: () => ({ submitWorkspaceInput: async (id, body) => {
        patrolPosts.push({ id, body });
        return { loop_id: id, accepted_sequence: patrolPosts.length };
      } }) },
    },
  });
  harness.context.fetch = async (url, options = {}) => {
    harness.fetches.push({ url, options });
    let body = [];
    if (url.endsWith("/settings/models")) body = { models: [] };
    if (url.endsWith("/workspaces")) body = { workspace_id: "ws" };
    if (url.endsWith("/threads")) body = { task_id: "new-task", workspace_id: "ws" };
    return { ok: true, status: 200, json: async () => body };
  };
  vm.runInContext(readAppSource(), harness.context);
  // 真实装配先完成恢复读取；本组仅用内存仓库隔离文件模式断言。
  await vm.runInContext(`(async () => {
    window.focusDesktop = { ...window.focusDesktop, workspacePatrolRecovery: {read:()=>({}),save:async()=>{}} };
    workspacePatrolIntake = await createWorkspacePatrolIntake();
  })()`, harness.context);
  return { ...harness, select, settings, patrolPosts,
    run: source => vm.runInContext(source, harness.context),
    async change(value) {
      select.value = value;
      harness.dispatch("change", { target: select });
      await new Promise(resolve => setImmediate(resolve));
    },
  };
}

test("settings change persists all modes and reopening restores the saved selection", async () => {
  const h = await setup();
  h.run("state.details.set('existing', { ui_state: { access_mode: 'workspace-write' } })");
  await h.run("openSettings()");
  assert.equal(h.select.value, "workspace-write");
  for (const mode of ["read-only", "danger-full-access", "workspace-write"]) {
    await h.change(mode);
    assert.equal(h.context.localStorage.getItem(storageKey), mode);
    h.settings.open = false;
    h.select.value = "";
    await h.run("openSettings()");
    assert.equal(h.select.value, mode);
    assert.equal(h.run("state.details.get('existing').ui_state.access_mode"), "workspace-write");
  }
});

test("ordinary task creation uses the mode saved by the settings event", async () => {
  const h = await setup();
  // 隔离创建之后的详情加载和页面绘制，保留真实 createTask 和 API 请求。
  h.run("hydrateActive = async () => {}; render = () => {};");
  h.context.taskForm = { querySelector: selector => ({
    "#workspacePath": { value: "C:/workspace" }, "#threadTitle": { value: "只读任务" },
  })[selector] || null };
  for (const mode of ["read-only", "danger-full-access", "workspace-write"]) {
    await h.change(mode);
    await h.run("createTask(taskForm)");
    const request = h.fetches.filter(item => item.url.endsWith("/threads")).at(-1);
    assert.equal(JSON.parse(request.options.body).access_mode, mode);
  }
});

test("existing Patrol stores read the saved default for each new submission and freeze retries", async () => {
  const h = await setup();
  h.run("workspacePatrolIntake.store('ws').edit({ content: '先写草稿' })");
  // 先直接保存以独立检验 Patrol 是否仍缓存启动时默认值。
  h.run("accessMode.saveNewSessionDefault(localStorage, 'read-only')");
  await h.run("workspacePatrolIntake.submit('ws')");
  assert.equal(h.patrolPosts[0].body.access_mode, "read-only");
  h.run(`
    workspacePatrolIntake.store('ws').edit({ content: '待重试' });
    globalThis.failed = workspacePatrolIntake.store('ws').submit();
    workspacePatrolIntake.store('ws').failed(failed.submission_id, new Error('offline'));
  `);
  await h.change("workspace-write");
  await h.run("workspacePatrolIntake.retry('ws', failed.submission_id)");
  assert.equal(h.patrolPosts[1].body.access_mode, "read-only");
  h.run("workspacePatrolIntake.store('ws').edit({ content: '新会话' })");
  await h.run("workspacePatrolIntake.submit('ws')");
  assert.equal(h.patrolPosts[2].body.access_mode, "workspace-write");
});

test("storage failure restores the saved selection and reports the error", async () => {
  const h = await setup();
  h.context.localStorage.setItem(storageKey, "read-only");
  h.context.localStorage.setItem = () => { throw new Error("storage unavailable"); };
  await h.change("workspace-write");
  assert.equal(h.context.localStorage.getItem(storageKey), "read-only");
  assert.equal(h.select.value, "read-only");
  assert.match(h.statusState.text, /storage unavailable/);
  assert.equal(h.statusState.danger, true);
});
