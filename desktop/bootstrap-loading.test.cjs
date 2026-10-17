/* 首屏、按入口读取及延迟失败的回归；运行 node --test desktop/bootstrap-loading.test.cjs。 */
"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

function harness() {
  const app = { innerHTML: "Patrol skeleton" };
  const status = { textContent: "", classList: { toggle() {} }, insertAdjacentHTML() {} };
  const result = createAppHarness({ selectors: { "#app": app, "#globalStatus": status } });
  result.vm.runInContext(readAppSource(), result.context);
  const run = source => result.vm.runInContext(source, result.context);
  // 此组只检查 HTTP bootstrap；恢复初始化由真实页面刷新测试覆盖。
  run(`workspacePatrolIntake = window.FocusWorkspacePatrolIntake.create({runtime, api:loopApi});`);
  run(`var renders = []; render = () => { renders.push(state.view); };`);
  return { run, app, status };
}

test("Patrol renders before bootstrap and requests no task details, plugins or trees", async () => {
  const { run } = harness();
  run(`var paths = [], releaseBootstrap;
    api = path => { paths.push(path);
      if (path.endsWith('/settings/models')) return Promise.resolve({setup_required:false});
      return new Promise(resolve => { releaseBootstrap = resolve; }); };
    var loading = bootstrap();`);
  assert.equal(run("renders[0]"), "patrol");
  assert.deepEqual(Array.from(run("paths")), ["/desktop/api/settings/models", "/desktop/api/bootstrap"]);
  run(`releaseBootstrap({tasks:[{task_id:'task',workspace_id:'ws'}], equipment:{tools:[],tools_status:'deferred'}});`);
  assert.equal(await run("loading"), true);
  assert.equal(run("state.activeTaskId"), "task");
  assert.deepEqual(Array.from(run("paths")), ["/desktop/api/settings/models", "/desktop/api/bootstrap"]);
});

test("a slow equipment connection and its failure leave Patrol usable, and allow retry", async () => {
  const { run, status } = harness();
  run(`var failEquipment, paths = [];
    api = path => { paths.push(path);
      if (path.endsWith('/equipment')) return new Promise((_resolve, reject) => { failEquipment = reject; });
      return Promise.resolve({tasks:[],equipment:{models:[],tools:[],tools_status:'deferred'}});
    };
    var tools = hydrateEquipment(); var loading = bootstrap();`);
  assert.equal(await run("loading"), true);
  assert.equal(run("state.view"), "patrol");
  run(`failEquipment(new Error('MCP unavailable'));`);
  await run("tools");
  assert.equal(status.textContent, "");
  assert.equal(run("state.equipment.tools_status"), "error");
  assert.equal(run("state.view"), "patrol");
  run(`api = async () => ({tools:[{name:'remote',source:'mcp'}]});`);
  await run("hydrateEquipment()");
  assert.equal(run("state.equipment.tools[0].name"), "remote");
  assert.equal(run("state.equipment.tools_status"), "loaded");
});

test("bootstrap failure keeps the existing Patrol and navigation intact", async () => {
  const { run, app, status } = harness();
  run(`api = async () => { throw new Error('offline'); };`);
  assert.equal(await run("bootstrap()"), false);
  assert.equal(app.innerHTML, "Patrol skeleton");
  assert.equal(status.textContent, "offline");
  assert.equal(run("state.view"), "patrol");
});

test("late bootstrap uses the current destination without returning to Patrol", async () => {
  const { run } = harness();
  run(`var releaseBootstrap, details = 0;
    api = path => path.endsWith('/settings/models') ? Promise.resolve({setup_required:false})
      : new Promise(resolve => { releaseBootstrap = resolve; });
    hydrateActive = async () => { details++; };
    var loading = bootstrap();
    state.view = 'focus';
    releaseBootstrap({tasks:[{task_id:'task'}],equipment:{tools:[]}});`);
  await run("loading");
  assert.equal(run("state.view"), "focus");
  assert.equal(run("details"), 1);
});

test("a slow model setup check does not delay the task bootstrap", async () => {
  const { run } = harness();
  run(`api = path => path.endsWith('/settings/models') ? new Promise(() => {})
    : Promise.resolve({tasks:[],equipment:{models:[],tools:[]}});`);
  assert.equal(await run("bootstrap()"), true);
  assert.equal(run("state.view"), "patrol");
});

test("trees load only for the current entry and omit inactive workspaces", async () => {
  const { run } = harness();
  run(`state.tasks = [{task_id:'task',workspace_id:'one'}, {task_id:'other',workspace_id:'two'},
    {task_id:'old',workspace_id:'archived',lifecycle:'archived'}];
    state.activeTaskId = 'task'; var paths = [];
    api = async path => { paths.push(path); return []; };`);
  await run("hydrateContextTrees()");
  assert.equal(run("paths.length"), 0);
  await run(`state.view='focus'; hydrateContextTrees()`);
  assert.deepEqual(Array.from(run("paths")), ["/desktop/api/workspaces/one/contexts/tree"]);
  await run(`paths=[]; state.view='map'; state.mapViewMode='tree'; hydrateContextTrees()`);
  assert.deepEqual(Array.from(run("paths")), [
    "/desktop/api/workspaces/one/contexts/tree", "/desktop/api/workspaces/two/contexts/tree",
  ]);
  await run(`paths=[]; state.mapViewMode='graph'; hydrateContextTrees()`);
  assert.equal(run("paths.length"), 0);
});

test("a late plugin discovery failure cannot replace Patrol or its status", async () => {
  const { run, app, status } = harness();
  run(`var failPlugins; state.view='focus';
    api = () => new Promise((_resolve, reject) => { failPlugins=reject; });
    var assets=ensurePluginAssets(); state.view='patrol'; failPlugins(new Error('plugin offline'));`);
  await run("assets");
  assert.equal(app.innerHTML, "Patrol skeleton");
  assert.equal(status.textContent, "");
  assert.equal(run("pluginAssetsLoading"), null);
});

test("late task details cannot open a recovery panel after returning to Patrol", async () => {
  const { run } = harness();
  run(`state.activeTaskId='task'; state.view='focus'; var releaseDetail, recoveries=0;
    api = path => {
      if (path === '/desktop/api/tasks/task') return new Promise(resolve => { releaseDetail=resolve; });
      return Promise.resolve(path.endsWith('/skills') ? {skills:[]} : []);
    };
    reconcileCommitmentRecovery = reconcileCompressionRecovery = reconcileAccessReview = () => { recoveries++; };
    updatePatrolAvatarLayer = () => {};
    var loading = hydrateActive(); state.view='patrol'; releaseDetail({messages:[]});`);
  await run("loading");
  assert.equal(run("state.details.has('task')"), true);
  assert.equal(run("recoveries"), 0);
  assert.equal(run("state.view"), "patrol");
});
