/*
 * 本文件验证设置面板中的模型区块。输入为生效目录快照、草稿编辑、字段级校验失败、引用确认与
 * 连通性测试结果；输出为列表/编辑器标记、不预填密钥、按字段呈现错误、保存后就地刷新装备，以及
 * 删除与恢复发行默认的本地目录变化。具体工作流在 VM 中直接调用 app.js 的渲染与提交函数。
 * 示例：node desktop/model-settings-ui.test.cjs。
 */
"use strict";

const assert = require("node:assert/strict");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

const host = { innerHTML: "" };
const harness = createAppHarness({ selectors: { "#modelSettingsHost": host } });
harness.vm.runInContext(readAppSource(), harness.context);

const SNAPSHOT = `
  const snapshot = {
    models: [
      {
        name: "alpha", display_name: "Alpha", use: "focus.models.deepseek:DeepSeekChatOpenAI",
        model: "alpha", api_key: "$OPENAI_API_KEY", api_key_variable: "OPENAI_API_KEY",
        api_key_set: true, api_key_literal: false, base_url: "https://api.example.com",
        context_window: 8192, curation_output_method: "prompt_json", curation_max_output_tokens: 8192,
        curation_default: true, default: true, supports_image_input: true, source: "shipped",
      },
      {
        name: "gamma", display_name: "Gamma", use: "langchain_openai:ChatOpenAI",
        model: "gamma", api_key: "$OPENAI_API_KEY", api_key_variable: "OPENAI_API_KEY",
        api_key_set: true, api_key_literal: false, base_url: "https://api.example.com",
        context_window: 4096, curation_output_method: null, curation_max_output_tokens: 8192,
        curation_default: false, default: false, supports_image_input: false, source: "user_added",
      },
    ],
    shipped_models: [
      {
        name: "alpha", display_name: "Alpha", use: "focus.models.deepseek:DeepSeekChatOpenAI",
        model: "alpha", api_key: "$OPENAI_API_KEY", base_url: "https://api.example.com",
        context_window: 8192, curation_output_method: "prompt_json", curation_max_output_tokens: 8192,
        curation_default: true, default: true, supports_image_input: true,
      },
    ],
    editable_fields: ["name", "display_name", "use", "model", "api_key", "base_url", "context_window",
      "curation_output_method", "curation_max_output_tokens", "curation_default", "default", "supports_image_input"],
    removed_models: [],
    adapters: [
      { use: "focus.models.deepseek:DeepSeekChatOpenAI", label: "DeepSeek（OpenAI 兼容）" },
      { use: "langchain_openai:ChatOpenAI", label: "OpenAI 兼容（通用）" },
    ],
    curation_output_methods: ["json_schema", "json_mode", "prompt_json"],
    default_model_name: "alpha", default_error: null, curation_default_model_name: "alpha",
    config_file: "config.yaml",
  };
`;

(async () => {
  const result = await harness.vm.runInContext(`(async () => {
    ${SNAPSHOT}
    modelSettings.snapshot = snapshot;
    modelSettings.draft = null;
    modelSettings.errors = {};
    modelSettings.dirty = false;
    renderModelSettings();
    const listHtml = document.querySelector("#modelSettingsHost").innerHTML;

    startModelDraft(0);
    const editorHtml = document.querySelector("#modelSettingsHost").innerHTML;

    modelSettings.errors = { "models[0].base_url": "必须是 http 或 https 的绝对地址" };
    renderModelSettings();
    const errorHtml = document.querySelector("#modelSettingsHost").innerHTML;

    modelSettings.errors = {};
    const saved = [];
    api = async (path, options) => { saved.push({ path, body: JSON.parse(options.body) }); return snapshot; };
    await saveModelSettings();
    const afterSave = {
      equipmentModels: state.equipment.models.map(entry => entry.name),
      dirty: modelSettings.dirty,
      payload: saved[0],
    };

    const rejected = [];
    api = async (path, options) => {
      rejected.push(JSON.parse(options.body));
      const error = new Error("invalid");
      error.status = 422;
      error.detail = { code: "model_settings_invalid", errors: { "models[0].name": "必填" } };
      throw error;
    };
    modelSettings.errors = {};
    modelSettings.draft = { index: 0, isNew: false, before: null };
    await saveModelSettings();
    const validationErrors = { ...modelSettings.errors };

    const confirmations = [];
    const attempted = [];
    api = async (path, options) => {
      const body = JSON.parse(options.body);
      attempted.push(Boolean(body.acknowledge_references));
      if (!body.acknowledge_references) {
        const error = new Error("referenced");
        error.status = 409;
        error.detail = { code: "model_referenced", references: { gamma: [{ kind: "run", run_id: "r1" }] } };
        throw error;
      }
      return snapshot;
    };
    confirm = message => { confirmations.push(message); return true; };
    modelSettings.snapshot = JSON.parse(JSON.stringify(snapshot));
    modelSettings.errors = {};
    await saveModelSettings();

    modelSettings.snapshot = JSON.parse(JSON.stringify(snapshot));
    removeModelEntry(1);
    const afterDelete = modelSettings.snapshot.models.map(entry => entry.name);

    confirm = () => true;
    modelSettings.snapshot = JSON.parse(JSON.stringify(snapshot));
    restoreShippedModels();
    const afterReset = modelSettings.snapshot.models.map(entry => entry.name);

    const probes = [];
    api = async (path, options) => { probes.push({ path, body: JSON.parse(options.body) }); return { ok: true, reason: "连接成功" }; };
    modelSettings.snapshot = JSON.parse(JSON.stringify(snapshot));
    startModelDraft(0);
    modelSettings.credentialValue = "typed-secret";
    await testModelConnection();
    const probeHtml = document.querySelector("#modelSettingsHost").innerHTML;

    api = async () => { throw new Error("should not be called"); };
    const addTemplate = snapshot.shipped_models[0];
    addModelEntry();
    const addedName = modelSettings.snapshot.models[modelSettings.snapshot.models.length - 1].use;
    cancelModelDraft();
    const afterCancel = modelSettings.snapshot.models.length;

    modelSettings.snapshot = JSON.parse(JSON.stringify(snapshot));
    modelSettings.snapshot.models = [];
    modelSettings.draft = null;
    renderModelSettings();
    const emptyHtml = document.querySelector("#modelSettingsHost").innerHTML;

    modelSettings.snapshot = JSON.parse(JSON.stringify(snapshot));
    modelSettings.snapshot.default_model_override = "deepseek-v4-flash-vision-exp";
    modelSettings.draft = null;
    renderModelSettings();
    const overrideHtml = document.querySelector("#modelSettingsHost").innerHTML;

    return {
      listHtml, editorHtml, errorHtml, afterSave, validationErrors, attempted, confirmations,
      afterDelete, afterReset, probeHtml, probes, addedName, addTemplate: addTemplate.use,
      afterCancel, emptyHtml, overrideHtml,
    };
  })()`, harness.context);

  // 列表：动作入口、来源标注、密钥状态，且不预填密钥
  assert.match(result.listHtml, /data-action="model-add"/);
  assert.match(result.listHtml, /data-action="model-save"/);
  assert.match(result.listHtml, /data-action="model-reset"/);
  assert.match(result.listHtml, /data-action="model-edit" data-model-index="0"/);
  assert.match(result.listHtml, /data-action="model-remove" data-model-index="1"/);
  assert.match(result.listHtml, /Alpha/);
  assert.match(result.listHtml, /Gamma/);
  assert.match(result.listHtml, /发行自带/);
  assert.match(result.listHtml, /你新增的/);
  assert.match(result.listHtml, /已设置/);
  assert.match(result.listHtml, /默认模型/);
  assert.match(result.listHtml, /策展默认模型/);
  assert.doesNotMatch(result.listHtml, /typed-secret/);

  // 编辑器：全部可编辑字段都在，密钥输入框永远为空
  for (const field of ["name", "display_name", "use", "model", "api_key", "base_url", "context_window",
    "curation_output_method", "curation_max_output_tokens", "default", "curation_default", "supports_image_input"]) {
    assert.match(result.editorHtml, new RegExp(`data-model-field="${field}"`), `缺少字段 ${field}`);
  }
  assert.match(result.editorHtml, /type="password" data-model-credential value=""/);
  assert.match(result.editorHtml, /value="\$OPENAI_API_KEY"/);
  assert.doesNotMatch(result.editorHtml, /sk-[a-z]/i);

  // 字段级错误就地呈现
  assert.match(result.errorHtml, /必须是 http 或 https 的绝对地址/);
  assert.match(result.errorHtml, /has-error/);
  assert.match(result.errorHtml, /settings-model-errors/);

  // 保存：只提交可编辑字段、就地刷新装备、清掉未保存标记
  assert.equal(result.afterSave.payload.path, "/desktop/api/settings/models");
  assert.deepEqual([...result.afterSave.equipmentModels], ["alpha", "gamma"]);
  assert.equal(result.afterSave.dirty, false);
  assert.deepEqual(Object.keys(result.afterSave.payload.body.models[0]).sort(),
    SNAPSHOT_EDITABLE_FIELDS.slice().sort());
  assert.equal(result.afterSave.payload.body.models[0].api_key, "$OPENAI_API_KEY");
  assert.equal(result.afterSave.payload.body.acknowledge_references, undefined);

  // 校验失败：错误按字段保存下来，且没有第二次写入
  assert.deepEqual({ ...result.validationErrors }, { "models[0].name": "必填" });

  // 引用确认：先被 409 拦住，确认后带 acknowledge_references 重试
  assert.deepEqual([...result.attempted], [false, true]);
  assert.match(result.confirmations[0], /gamma/);

  // 删除与恢复发行默认只改本地目录，等待保存提交
  assert.deepEqual([...result.afterDelete], ["alpha"]);
  assert.deepEqual([...result.afterReset], ["alpha"]);

  // 连通性测试：用输入框里的密钥，结果以横幅呈现
  assert.equal(result.probes.length, 1);
  assert.equal(result.probes[0].path, "/desktop/api/settings/models/test");
  assert.equal(result.probes[0].body.api_key_value, "typed-secret");
  assert.match(result.probeHtml, /连接成功/);
  // 用户自己敲进密码框的值会在重渲染后原样留在框里：可见状态必须等于将要提交的内容，
  // 否则「看起来空的、保存却提交了旧输入」会让用户在没打算轮换密钥时换掉密钥
  assert.match(result.probeHtml, /type="password" data-model-credential value="typed-secret"/);

  // 新增条目沿用适配器默认值，取消后不留残余
  assert.equal(result.addedName, result.addTemplate);
  assert.equal(result.afterCancel, 2);

  // 空目录要给出可理解的引导，而不是空白
  assert.match(result.emptyHtml, /当前没有可用模型条目/);
  assert.match(result.emptyHtml, /data-action="model-add"/);

  // 环境变量覆盖默认模型时必须显式告知：否则「默认模型」徽标会骗人
  assert.match(result.overrideHtml, /FOCUS_MODEL 正在覆盖默认模型：deepseek-v4-flash-vision-exp/);

  console.log("model-settings-ui: all assertions passed");
})().catch(error => { console.error(error); process.exitCode = 1; });

const SNAPSHOT_EDITABLE_FIELDS = ["name", "display_name", "use", "model", "api_key", "base_url",
  "context_window", "curation_output_method", "curation_max_output_tokens", "curation_default",
  "default", "supports_image_input"];
