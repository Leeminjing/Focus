/*
 * 本文件对外提供 Agent Loop 授权错误邻接展示与表单重试 identity 的 DOM 回归测试。
 * 输入为可授权 Run、结构化所有权冲突和已填写 Mission 表单；输出为按钮旁稳定错误、保留表单值及 loop/activation retry identity 的断言。
 * 具体工作流为在 app.js VM 中调用真实 startAgentLoop，注入确定性 Loop API 冲突并检查原表单节点未被重建或清空。
 * 示例：`node --test desktop/loop-authorization-error.test.cjs`。
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");


test("authorization conflict stays beside the button and preserves mission plus retry identity", async () => {
  const status = { textContent: "" };
  const submit = { disabled: false, isConnected: true };
  const loopApi = {
    activationEligibility: async () => ({ eligible: true, candidate_run_id: "run-new", consistency_token: "ready-token" }),
    start: async () => {
      const error = new Error("Context 已由 Loop loop-old 的 Lane lane-old 持有（HTTP 409）");
      error.status = 409;
      error.detail = { code: "curation_ownership_conflict", owner_loop_id: "loop-old", lane_id: "lane-old" };
      throw error;
    },
  };
  class TestFormData {
    constructor(form) { this.form = form; }
    get(name) { return this.form.values[name] ?? null; }
  }
  const harness = createAppHarness({
    globals: {
      FormData: TestFormData,
      FocusLoopApi: { create: () => loopApi },
      FocusLoopStore: { create: () => ({ load() {}, fail() {} }) },
      FocusLoopMissionEditor: { read: form => ({ outcome: form.values.outcome, boundaries: {}, completion_checks: [] }) },
    },
  });
  harness.vm.runInContext(readAppSource(), harness.context);
  const form = {
    dataset: {},
    values: { outcome: "保留我的 Mission", autonomousCompression: "on", isolatedWrites: "off" },
    querySelector(selector) {
      if (selector === 'button[type="submit"]') return submit;
      if (selector === "[data-loop-start-status]") return status;
      return null;
    },
  };
  harness.context.__form = form;
  await harness.vm.runInContext(`(async () => {
    state.tasks = [{ task_id: "context-1", workspace_id: "workspace-1", title: "Context" }];
    state.activeTaskId = "context-1";
    state.details.set("context-1", { ui_state: { permissions: ["read"], skills: [] } });
    await startAgentLoop(__form);
  })()`, harness.context);
  assert.match(status.textContent, /授权失败/);
  assert.match(status.textContent, /loop-old/);
  assert.match(status.textContent, /lane-old/);
  assert.doesNotMatch(status.textContent, /Unexpected token/);
  assert.equal(form.values.outcome, "保留我的 Mission");
  assert.match(form.dataset.loopId, /^[a-f0-9]{32}$/);
  assert.equal(form.dataset.activationKey, "loop-activation:run-new");
  assert.equal(submit.disabled, false);
});
