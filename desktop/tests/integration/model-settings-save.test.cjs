/* 回归：默认项互斥、不同凭据来源的草稿在切换编辑后一起保存。 */
"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { createAppHarness, readAppSource } = require("../helpers/test-helper.cjs");

function setup() {
  const host = { innerHTML: "" };
  const harness = createAppHarness({ selectors: { "#modelSettingsHost": host } });
  harness.vm.runInContext(readAppSource(), harness.context);
  harness.vm.runInContext(`
    modelSettings.snapshot = {
      models: [
        { name: "alpha", api_key: "$ALPHA_KEY", default: true, curation_default: true },
        { name: "beta", api_key: "$BETA_KEY", default: false, curation_default: false },
      ],
      editable_fields: ["name", "api_key", "default", "curation_default"],
    };
  `, harness.context);
  return {
    ...harness, host, run: source => harness.vm.runInContext(source, harness.context),
    typeKey: value => harness.dispatch("input", {
      target: { value, matches: selector => selector === "[data-model-credential]" },
    }),
  };
}

for (const field of ["default", "curation_default"]) {
  test(`selecting ${field} replaces the old selection`, () => {
    const h = setup();
    const selected = h.run(`
      startModelDraft(1);
      updateModelField({ dataset: { modelField: "${field}" }, checked: true });
      modelSettings.snapshot.models.filter(entry => entry.${field}).map(entry => entry.name);
    `);
    assert.deepEqual([...selected], ["beta"]);
    const restored = h.run(`
      cancelModelDraft();
      modelSettings.snapshot.models.filter(entry => entry.${field}).map(entry => entry.name);
    `);
    assert.deepEqual([...restored], ["alpha"], "cancel restores the previous default");
  });
}

test("keys for both providers survive switching and are saved together", async () => {
  const h = setup();
  h.run("startModelDraft(0)");
  h.typeKey("alpha-secret");
  assert.equal(h.run("modelSettings.dirty"), true);
  h.run("startModelDraft(1)");
  h.typeKey("beta-secret");
  h.run("startModelDraft(0)");
  assert.match(h.host.innerHTML, /data-model-credential value="alpha-secret"/);
  const payload = await h.run(`(async () => {
    let submitted;
    api = async (_path, options) => {
      submitted = JSON.parse(options.body);
      return modelSettings.snapshot;
    };
    await saveModelSettings();
    return submitted;
  })()`);
  assert.deepEqual({ ...payload.credentials }, { ALPHA_KEY: "alpha-secret", BETA_KEY: "beta-secret" });
  assert.deepEqual({ ...h.run("pendingModelCredentials()") }, {}, "successful save clears secrets");
});

test("credential source edits select their own drafts without moving secrets", () => {
  const h = setup();
  h.run("startModelDraft(0)");
  h.typeKey("alpha-secret");
  h.run('updateModelField({ dataset: { modelField: "api_key" }, value: "$BETA_KEY" })');
  assert.equal(h.run("modelCredentialValue()"), "");
  h.typeKey("beta-secret");
  h.run('updateModelField({ dataset: { modelField: "api_key" }, value: "${ALPHA_KEY}" })');
  assert.equal(h.run("modelCredentialValue()"), "alpha-secret");
  h.run("startModelDraft(1)");
  assert.match(h.host.innerHTML, /data-model-credential value="beta-secret"/);
});

test("shared references use one draft and clearing it leaves the stored key alone", () => {
  const h = setup();
  h.run('modelSettings.snapshot.models[1].api_key = "${ALPHA_KEY}"; startModelDraft(0)');
  h.typeKey("shared-secret");
  h.run("startModelDraft(1)");
  assert.match(h.host.innerHTML, /data-model-credential value="shared-secret"/);
  h.typeKey("");
  assert.deepEqual({ ...h.run("pendingModelCredentials()") }, {});
  h.run("startModelDraft(0)");
  assert.equal(h.run("modelCredentialValue()"), "");
});

test("cancel discards the current key edit and retains other sources", () => {
  const h = setup();
  h.run("startModelDraft(0)");
  h.typeKey("alpha-secret");
  h.run("startModelDraft(1)");
  h.typeKey("cancelled-secret");
  h.run("cancelModelDraft()");
  assert.deepEqual({ ...h.run("pendingModelCredentials()") }, { ALPHA_KEY: "alpha-secret" });
});

test("failed saves preserve all keys for a retry", async () => {
  const h = setup();
  h.run("startModelDraft(0)");
  h.typeKey("alpha-secret");
  h.run("startModelDraft(1)");
  h.typeKey("beta-secret");
  await h.run(`(async () => {
    api = async () => { throw Object.assign(new Error("invalid"), {
      status: 422, detail: { errors: { default: "invalid" } },
    }); };
    await saveModelSettings();
  })()`);
  assert.deepEqual({ ...h.run("pendingModelCredentials()") }, { ALPHA_KEY: "alpha-secret", BETA_KEY: "beta-secret" });
  assert.match(h.host.innerHTML, /data-model-credential value="beta-secret"/);
});

test("removing a model excludes its unused key while preserving shared keys", () => {
  const h = setup();
  h.run("startModelDraft(0)");
  h.typeKey("alpha-secret");
  h.run("startModelDraft(1)");
  h.typeKey("beta-secret");
  h.run("removeModelEntry(1)");
  assert.deepEqual({ ...h.run("pendingModelCredentials()") }, { ALPHA_KEY: "alpha-secret" });
  h.run(`
    modelSettings.snapshot.shipped_models = modelSettings.snapshot.models;
    confirm = () => true;
    restoreShippedModels();
  `);
  assert.deepEqual({ ...h.run("pendingModelCredentials()") }, {});
});

test("setup opens the required model and labels other unconfigured entries as optional", async () => {
  const h = setup();
  const index = await h.run(`(async () => {
    modelSettings.snapshot.models[0].default = false;
    modelSettings.snapshot.models[0].curation_default = false;
    modelSettings.snapshot.models[1].default = true;
    modelSettings.snapshot.models[1].curation_default = true;
    modelSettings.snapshot.setup_required = true;
    modelSettings.snapshot.setup_issues = { "models[1].api_key": "Missing key" };
    api = async () => modelSettings.snapshot;
    openSettings = async () => {};
    selectSettingsTab = () => {};
    await ensureModelSetup();
    return modelSettings.draft.index;
  })()`);
  assert.equal(index, 1);
  assert.match(h.host.innerHTML, /未配置（可选模板）/);
});
