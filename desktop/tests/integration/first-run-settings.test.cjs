/* 首次引导复用生产模型设置，只有保存后的完整验证通过才交还桌面入口。 */
"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { createAppHarness, createNode, readAppSource } = require("../helpers/test-helper.cjs");

function fixture() {
  const completed = [], host = createNode(), transitions = [];
  const harness = createAppHarness({ selectors: { "#modelSettingsHost": host }, globals: {
    focusDesktop: { runtime: () => ({}), patrol: { enabled: true, onWorkbench() {},
      completeModelSetup: async () => { completed.push(true); return true; } } },
    FocusSurfaceTransition: { visible: async (...args) => transitions.push(args) },
  } });
  const run = source => harness.vm.runInContext(source, harness.context);
  run(readAppSource());
  run(`openSettings = async () => {}; selectSettingsTab = () => {};
    var snapshot = {models:[{name:'first',api_key:'$FIRST_API_KEY',api_key_set:false,default:true}],editable_fields:[],shipped_models:[],setup_required:true};
    api = async () => snapshot;`);
  return { run, host, completed, transitions };
}

test("desktop setup preserves an unsaved key when the robot reopens settings", async () => {
  const f = fixture();
  await f.run("openPatrolWorkbench({setupModels:true})");
  f.run("updateModelCredential('unsaved');");
  await f.run("openPatrolWorkbench({setupModels:true})");
  assert.equal(f.run("modelCredentialValue()"), "unsaved");
  assert.match(f.host.innerHTML, /data-model-credential value="unsaved"/);
  assert.match(f.host.innerHTML, /选择项目文件夹/);
});

test("saving stays in setup until full verification and an explicit start", async () => {
  const f = fixture();
  await f.run("openPatrolWorkbench({setupModels:true})");
  f.run(`putModelSettings = async () => ({...snapshot,setup_required:false,verification:{ok:false,reason:'策展验证失败'}});`);
  await f.run("saveModelSettings()");
  assert.equal(f.completed.length, 0);
  assert.match(f.host.innerHTML, /策展验证失败/);
  f.run(`putModelSettings = async () => ({...snapshot,setup_required:false,verification:{ok:true,reason:'完整验证通过'}});`);
  await f.run("saveModelSettings()");
  assert.equal(f.completed.length, 0);
  assert.match(f.host.innerHTML, /data-action="model-start"/);
  await f.run(`handleDocumentClick({target:{closest: selector => selector === '[data-action]' ? {dataset:{action:'model-start'}} : null}})`);
  assert.equal(f.completed.length, 1);
  assert.equal(f.run("modelSettings.onboarding"), false);
  assert.equal(f.transitions.at(-1)[1], false);
});

test("setup status failure shows a retry and a saved but unverified config still needs verification", async () => {
  const f = fixture();
  f.run("api = async () => { throw new Error('HTTP 503'); };");
  await assert.rejects(f.run("openPatrolWorkbench({setupModels:true})"), /503/);
  assert.match(f.host.innerHTML, /model-setup-retry/);
  f.run("api = async () => ({...snapshot,setup_required:false});");
  await f.run("ensureModelSetup({onboarding:true})");
  await f.run("finishDesktopModelSetup()");
  assert.equal(f.completed.length, 0);
  assert.match(f.host.innerHTML, /保存并验证通过/);
});
