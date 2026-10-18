/* 首配验收：简化输入、高级修复、真实进度展示、保存与只读重试的边界。 */
"use strict";
const assert = require("node:assert/strict");
const { createAppHarness, createNode, readAppSource } = require("./test-helper.cjs");
const host = { innerHTML: "" };
let poll;
const harness = createAppHarness({ selectors: { "#modelSettingsHost": host, "#settingsDialog": createNode(), "#taskDialog": createNode() }, globals: {
  setInterval(callback) { poll = callback; return 1; }, clearInterval() { poll = null; },
} });
harness.vm.runInContext(readAppSource(), harness.context);
const run = source => harness.vm.runInContext(source, harness.context);

(async () => {
  await run(`
    const first = { name: "first", display_name: "First", use: "langchain_openai:ChatOpenAI",
      model: "test", provider: "openai", protocol: "chat_completions", api_key: "$TEST_KEY",
      base_url: "https://example.test/v1", default: true, curation_default: false,
      curation_output_method: null, api_key_set: false };
    const snapshot = { models: [first, { ...first, name: "second", default: false, curation_default: true }],
      shipped_models: [{ ...first }], setup_required: true, setup_issues: {},
      editable_fields: Object.keys(first).filter(key => key !== "api_key_set"),
      adapters: [], curation_output_methods: ["prompt_json"] };
    api = async () => JSON.parse(JSON.stringify(snapshot));
    openSettings = async () => {};
    selectSettingsTab = () => {};
    ensureModelSetup();
  `);
  const initial = host.innerHTML;
  const simple = initial.split('class="settings-model-grid is-simple"')[1].split('<div hidden>')[0];
  assert.match(simple, /data-model-service/);
  assert.match(simple, /data-model-credential/);
  assert.match(simple, /data-model-field="model"/);
  assert.doesNotMatch(simple, /data-model-field="(?:use|provider|protocol|api_key|context_window)"/);
  assert.match(initial, /aria-expanded="false"/);
  assert.match(initial, /验证并开始/);
  assert.doesNotMatch(initial, /data-action="model-start"/);
  assert.match(initial, /能否为后续工作检索上下文/);
  assert.equal(run('modelSettings.snapshot.models.filter(entry => entry.default && entry.curation_default).length'), 1);
  assert.equal(run('modelSettings.snapshot.models[1].curation_default'), false);

  run('modelSettings.credentialValue = "typed-secret"; updateModelService("openai");');
  assert.equal(run('modelSettings.credentialValue'), "typed-secret");
  assert.equal(run('modelSettings.snapshot.models[0].base_url'), "https://api.openai.com/v1");
  run('updateModelService("custom");');
  assert.equal(run('modelSettings.advanced'), true);
  assert.equal(run('modelSettings.snapshot.models[0].base_url'), "");

  await run(`
    modelSettings.advanced = false;
    modelSettings.credentialValue = "typed-secret";
    let resolveSave;
    let savedBody;
    const failed = { ...snapshot, setup_required: false, verification: { ok: false, status: "complete", reason: "策展失败",
      checks: [{ capability: "chat", model: "first", ok: true },
        { capability: "curation", model: "second", ok: false, reason: "修复输出方式 <script>" },
        { capability: "semantic_index", model: "second", ok: true }] } };
    api = async (path, options) => {
      if (options?.method === "PUT") {
        savedBody = JSON.parse(options.body);
        return new Promise(resolve => { resolveSave = resolve; });
      }
      return { verification: { verification_id: savedBody.verification_id, status: "running", checks: [
        { capability: "chat", status: "passed", ok: true },
        { capability: "curation", status: "running" }, { capability: "semantic_index", status: "pending" }] } };
    };
    const saving = saveModelSettings();
  `);
  assert.match(host.innerHTML, /<fieldset class="settings-model-controls" disabled>/);
  await poll();
  assert.match(host.innerHTML, /data-model-check="chat" data-check-status="passed"/);
  assert.match(host.innerHTML, /data-model-check="curation" data-check-status="running"/);
  assert.match(host.innerHTML, /data-model-check="semantic_index" data-check-status="pending"/);
  assert.equal(run('savedBody.credentials.TEST_KEY'), "typed-secret");
  assert.ok(run('savedBody.verification_id'));
  await run('resolveSave(failed); saving;');
  assert.equal(poll, null);
  assert.match(host.innerHTML, /data-action="model-repair" data-capability="curation"/);
  assert.match(host.innerHTML, /&lt;script&gt;/);
  assert.doesNotMatch(host.innerHTML, /data-action="model-start"/);
  run('repairModelCheck("curation");');
  assert.equal(run('modelSettings.draft.index'), 1);
  assert.equal(run('modelSettings.advanced'), true);
  assert.equal(run('modelSettings.credentialValue'), "");

  await run(`
    let retried;
    api = async (path, options) => { retried = { path, body: JSON.parse(options.body) };
      return { ...failed, verification: { ok: true, checks: failed.verification.checks.map(check => ({ ...check, ok: true, reason: undefined })) } }; };
    reverifyModelSettings();
  `);
  assert.equal(run('retried.path'), "/desktop/api/settings/models/verify");
  assert.deepEqual([...run('Object.keys(retried.body)')], ["verification_id"]);
  assert.match(host.innerHTML, /data-action="model-start"/);
  run('updateModelField({ dataset: { modelField: "model" }, value: "changed" }); renderModelSettings();');
  assert.doesNotMatch(host.innerHTML, /data-action="model-start"/);

  // 网络中断后不能继续展示「验证中」或沿用此前的成功结论。
  await run('modelSettings.dirty = false; api = async () => { throw new Error("offline"); }; reverifyModelSettings();');
  assert.match(host.innerHTML, /data-check-status="interrupted"/);
  assert.doesNotMatch(host.innerHTML, /data-action="model-start"/);
  assert.match(host.innerHTML, /data-action="model-verify"/);

  // 已配置过的独立策展默认不能在打开修复入口时被首配默认覆盖。
  await run(`
    modelSettings.setupChecked = false;
    api = async () => ({ ...snapshot, setup_required: false,
      models: snapshot.models.map(entry => ({ ...entry, api_key_set: true })), verification: failed.verification });
    ensureModelSetup();
  `);
  assert.equal(run('modelSettings.snapshot.models[1].curation_default'), true);
  assert.equal(run('modelSettings.snapshot.models[0].curation_default'), false);

  // 通过后的入口确实进入现有的新建工作区任务对话框。
  await run(`
    modelSettings.snapshot.verification = { ok: true };
    modelSettings.progress = null;
    modelSettings.dirty = false;
    const transitions = [];
    window.FocusSurfaceTransition = { visible: async (element, visible) => { transitions.push({ task: element === dialog, visible }); } };
    const startButton = { dataset: { action: "model-start" } };
    handleDocumentClick({ target: { closest: selector => selector === "[data-action]" ? startButton : null } });
  `);
  assert.deepEqual(JSON.parse(run('JSON.stringify(transitions)')), [{ task: false, visible: false }, { task: true, visible: true }]);
  console.log("model-setup-ui: all assertions passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
