/* 设置分别读取普通会话默认、Patrol 默认与工作区实效模式；查询失败不假冒默认值。 */
"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const AccessMode = require("../../src/renderer/features/conversation/access-mode.js");
const { createAppHarness, createNode, readAppSource } = require("../helpers/test-helper.cjs");

function fixture(read = async () => null) {
  const nodes = Object.fromEntries(["newSessionAccessMode", "newPatrolAccessMode", "currentPatrolAccessMode", "currentPatrolWorkspace", "newSessionModeFeedback"].map(id => [id, createNode({ textContent: "" })]));
  const dialog = createNode({ querySelector: selector => nodes[selector.slice(1)] || null });
  const reads = [];
  const h = createAppHarness({ selectors: { "#settingsDialog": dialog }, globals: {
    FocusAccessMode: AccessMode,
    FocusLoopApi: { create: () => ({ workspacePatrol: async id => { reads.push(id); return read(id); } }) },
  } });
  const run = code => h.vm.runInContext(code, h.context);
  run(readAppSource());
  run(`state.patrolWorkspace = {workspace_id:'a',name:'工作区 A',path:'C:/a'};`);
  return { ...h, nodes, reads, run };
}

test("settings read the current Patrol mode independently of both new defaults", async () => {
  for (const mode of AccessMode.MODES) {
    const f = fixture(async () => ({ loop_id: "loop", access_mode: mode }));
    f.context.localStorage.setItem("focus-new-session-file-mode-v1", "read-only");
    await f.run("loadFileModeSettings()");
    assert.equal(f.nodes.newSessionAccessMode.value, "read-only");
    assert.equal(f.nodes.currentPatrolAccessMode.textContent, AccessMode.labelFallback(mode));
    assert.equal(f.nodes.currentPatrolWorkspace.textContent, "工作区 A");
    assert.equal(f.nodes.newPatrolAccessMode.textContent, AccessMode.labelFallback("danger-full-access"));
    assert.deepEqual(f.reads, ["a"]);
    const select = f.nodes.newSessionAccessMode;
    select.id = "newSessionAccessMode"; select.value = "workspace-write";
    f.dispatch("change", { target: { ...select, matches: () => false } });
    assert.equal(f.context.localStorage.getItem("focus-new-session-file-mode-v1"), "workspace-write");
    assert.equal(f.nodes.currentPatrolAccessMode.textContent, AccessMode.labelFallback(mode));
    assert.equal(f.nodes.newSessionModeFeedback.textContent, "已保存普通会话新建默认值");
    assert.deepEqual(f.reads, ["a"], "changing the ordinary default never writes to Patrol");
  }
});

test("absent workspace, absent Patrol, unknown mode and query failure stay distinct", async () => {
  const f = fixture();
  f.run("state.patrolWorkspace = null");
  await f.run("loadFileModeSettings()");
  assert.equal(f.nodes.currentPatrolAccessMode.textContent, "未选择工作区");
  assert.equal(f.reads.length, 0);
  f.run("state.patrolWorkspace = {workspace_id:'a'}");
  await f.run("loadFileModeSettings()");
  assert.equal(f.nodes.currentPatrolAccessMode.textContent, "尚未创建");
  for (const mode of [null, undefined, "invalid"]) {
    const unknown = fixture(async () => ({ loop_id: "loop", access_mode: mode }));
    await unknown.run("loadFileModeSettings()");
    assert.equal(unknown.nodes.currentPatrolAccessMode.textContent, "实际模式不可用");
  }
  let failed = true;
  const retry = fixture(async () => { if (failed) throw new Error("offline"); return { access_mode: "read-only" }; });
  await retry.run("loadFileModeSettings()");
  assert.equal(retry.nodes.currentPatrolAccessMode.textContent, "读取失败，请刷新重试");
  failed = false;
  await retry.run(`handleDocumentClick({target:{closest: selector => selector === '[data-action]' ? {dataset:{action:'refresh-file-modes'}} : null}})`);
  assert.equal(retry.nodes.currentPatrolAccessMode.textContent, "只读执行");
});

test("a late workspace response cannot overwrite a newer settings read", async () => {
  let resolve;
  const f = fixture(id => id === "a" ? new Promise(done => { resolve = done; }) : { access_mode: "workspace-write" });
  const old = f.run("loadFileModeSettings()");
  assert.equal(f.nodes.currentPatrolAccessMode.textContent, "正在读取实际模式…");
  f.run("state.patrolWorkspace = {workspace_id:'b',name:'B'}");
  await f.run("loadFileModeSettings()");
  resolve({ access_mode: "danger-full-access" });
  await old;
  assert.equal(f.nodes.currentPatrolAccessMode.textContent, "工作区可写");
  assert.equal(f.nodes.currentPatrolWorkspace.textContent, "B");
});
