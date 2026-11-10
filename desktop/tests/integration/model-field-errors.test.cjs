/* 模型卡不推断连通性；保存与单条测试错误映射到可见、具名且可定位的输入。 */
"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { createAppHarness, readAppSource } = require("../helpers/test-helper.cjs");
const { createElement } = require("../helpers/test-dom.cjs");

function fixture() {
  const host = createElement("div");
  const harness = createAppHarness({ selectors: { "#modelSettingsHost": host } });
  const run = code => harness.vm.runInContext(code, harness.context);
  run(readAppSource());
  let focused;
  harness.context.document.querySelectorAll = selector => {
    const nodes = host.querySelectorAll(selector);
    for (const node of nodes) node.focus = () => { focused = node.getAttribute('id'); };
    return nodes;
  };
  run(`modelSettings.snapshot = {
    models: ['alpha', 'beta', 'gamma'].map((name, index) => ({ name, display_name: name,
      use: 'langchain_openai:ChatOpenAI', model: name, provider: 'openai', protocol: 'chat_completions',
      api_key: '$TEST_KEY', api_key_set: true, base_url: 'https://fixture.invalid/v1', context_window: 8192,
      default: index === 0, curation_default: index === 0, curation_output_method: 'prompt_json' })),
    editable_fields: ['name', 'model', 'use', 'api_key', 'base_url', 'provider', 'protocol'],
    adapters: [{use:'langchain_openai:ChatOpenAI', label:'OpenAI'}], shipped_models: [],
    curation_output_methods: ['prompt_json'], setup_required: false,
  }; renderModelSettings();`);
  return { host, run, focused: () => focused };
}

test("cards show verified results only for the checked model and keep technical fields in editing", () => {
  const f = fixture();
  assert.doesNotMatch(f.host.textContent, /fixture\.invalid|8192|发行自带|chat_completions/);
  assert.equal(f.host.querySelectorAll('[data-model-availability="pending"]').length, 3);
  f.run(`modelSettings.snapshot.verification = {checks:[
    {capability:'chat', model:'beta', ok:true}, {capability:'curation',model:'alpha',ok:false}]};
    renderModelSettings();`);
  const rows = f.host.querySelectorAll('.settings-model-row');
  assert.match(rows[0].textContent, /验证未通过/);
  assert.match(rows[1].textContent, /已通过验证/);
  assert.match(rows[2].textContent, /未验证/);
  f.run("modelSettings.snapshot.models[1].provider = null; modelSettings.snapshot.models[1].effective_provider = 'openai'; startModelDraft(1)");
  const editor = f.host.querySelector('#modelEditor-1');
  assert.ok(editor);
  assert.match(editor.textContent, /当前生效：openai/);
  for (const field of ['provider', 'protocol', 'base_url', 'context_window', 'curation_output_method', 'api_key']) {
    const input = editor.querySelector(`[data-model-field="${field}"]`);
    assert.ok(input, field);
    assert.ok(editor.querySelector(`label[for="${input.getAttribute('id')}"]`), `${field} must have an explicit label`);
  }
  assert.equal(f.host.querySelectorAll('[data-model-credential]').length, 1);
});

test("save errors open the affected model, focus its field and leave only catalog errors in the global region", async () => {
  const f = fixture();
  await f.run(`api = async () => {const error = new Error('invalid'); error.status = 422;
    error.detail = { errors: {'models[1].protocol':'协议不受支持', default:'必须选择一个默认模型'} }; throw error; };
    saveModelSettings();`);
  assert.equal(f.run("modelSettings.draft.index"), 1);
  const input = f.host.querySelector('#model-1-protocol');
  assert.equal(input.getAttribute('aria-invalid'), 'true');
  assert.equal(input.getAttribute('aria-describedby'), 'model-1-protocol-error');
  assert.equal(f.host.querySelector('#model-1-protocol-error').textContent, '协议不受支持');
  assert.equal(f.focused(), 'model-1-protocol');
  assert.equal(f.host.querySelector('.settings-model-errors').textContent, '必须选择一个默认模型');
  f.run("startModelDraft(0); startModelDraft(1)");
  assert.ok(f.host.querySelector('#model-1-protocol-error'), 'switching editors retains the error');
  f.run(`updateModelField({dataset:{modelField:'protocol'},value:'responses'})`);
  assert.equal(f.host.querySelector('#model-1-protocol').getAttribute('aria-invalid'), null);
  assert.equal(f.host.querySelector('#model-1-protocol-error'), null);
});

test("a single-model connection error maps models[0] to the actual editor and credential errors to the password input", async () => {
  const f = fixture();
  await f.run(`startModelDraft(2); api = async () => {const error = new Error('invalid'); error.status = 422;
    error.detail = {errors:{'models[0].api_key':'请输入有效凭据','models[0].base_url':'端点无效'}}; throw error;};
    testModelConnection();`);
  assert.equal(f.run("modelSettings.draft.index"), 2);
  assert.ok(f.host.querySelector('#model-2-api_key_value[aria-invalid="true"]'));
  assert.ok(f.host.querySelector('#model-2-base_url[aria-invalid="true"]'));
  assert.equal(f.host.querySelector('#model-2-api_key').getAttribute('aria-invalid'), null);
  assert.equal(f.host.querySelector('.settings-model-errors'), null);
  assert.equal(f.host.querySelector('#model-0-api_key_value-error'), null);
});

test("first setup opens advanced fields with the same error associations", async () => {
  const f = fixture();
  await f.run(`modelSettings.snapshot.setup_required = true;
    modelSettings.snapshot.setup_issues = {'models[0].protocol':'请选择有效协议'};
    api = async () => modelSettings.snapshot; openSettings = async () => {}; selectSettingsTab = () => {};
    ensureModelSetup();`);
  assert.equal(f.run('modelSettings.advanced'), true);
  assert.ok(f.host.querySelector('#model-0-protocol[aria-describedby="model-0-protocol-error"]'));
  assert.equal(f.host.querySelector('.settings-model-errors'), null);
  const ids = f.host.querySelectorAll('[id]').map(node => node.getAttribute('id'));
  assert.equal(new Set(ids).size, ids.length, 'each input and feedback has a unique ID');
});

test("an obsolete connection response cannot restore success after editing", async () => {
  const f = fixture();
  f.run(`startModelDraft(0); let resolveProbe; api = async () => new Promise(resolve => {resolveProbe = resolve});
    const pendingProbe = testModelConnection();
    updateModelField({dataset:{modelField:'base_url'},value:'https://changed.invalid'});`);
  await f.run("resolveProbe({ok:true}); pendingProbe");
  assert.equal(f.run('modelSettings.test'), null);
  assert.equal(f.host.querySelector('[data-model-connection-result]'), null);
  assert.doesNotMatch(f.host.querySelector('.settings-model-row').textContent, /连接成功|已通过验证/);
});

test("testing a pending credential reports connection success without pretending it was saved", async () => {
  const f = fixture();
  await f.run(`startModelDraft(1); updateModelCredential('pending-secret');
    api = async () => ({ok:true}); testModelConnection();`);
  const row = f.host.querySelectorAll('.settings-model-row')[1];
  assert.match(row.textContent, /连接成功/);
  assert.match(row.textContent, /密钥待保存/);
  assert.equal(f.run('modelSettings.dirty'), true);
});

test("removing a model keeps remaining field errors attached to their new indices", () => {
  const f = fixture();
  f.run(`modelSettings.errors = {'models[2].protocol':'协议无效'};
    modelSettings.snapshot.setup_issues = {'models[1].api_key':'凭据无效'};
    removeModelEntry(0); startModelDraft(1);`);
  assert.equal(f.run('modelSettings.errors["models[1].protocol"]'), '协议无效');
  assert.equal(f.run('modelSettings.snapshot.setup_issues["models[0].api_key"]'), '凭据无效');
  assert.ok(f.host.querySelector('#model-1-protocol-error'));
});
