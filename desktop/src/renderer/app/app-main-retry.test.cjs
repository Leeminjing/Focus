/* 普通任务的回执丢失恢复：执行真实 app.js 发送入口，模拟后端已受理但网络返回失败。 */
const assert = require("node:assert/strict");
const vm = require("node:vm");
const test = require("node:test");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { MainRequestStorage } = require("../../main/main-request-storage.cjs");
const { createAppHarness, createNode, readAppSource } = require("../../../tests/helpers/test-helper.cjs");

function setup(options = {}) {
  const input = createNode({ value: "检查这份附件" });
  const feedback = createNode(), recovery = createNode(), send = createNode();
  const harness = createAppHarness({ fetch: true, statusNode: "record", storage: options.storage,
    globals: options.globals,
    selectors: { "#mainInput": input, "#composerFeedback": feedback, "#mainSubmissionRecovery": recovery, '[data-action="send-main"]': send } });
  const evaluate = code => vm.runInContext(code, harness.context);
  evaluate(readAppSource());
  evaluate(`
    state.view = "focus";
    state.activeTaskId = "task";
    state.tasks = [{ task_id: "task", workspace_id: "workspace", workspace_path: "C:/workspace" }];
    state.details.set("task", { messages: [], ui_state: { input: "检查这份附件" } });
    composerDraft.claim("task", "检查这份附件");
    state.materials.set("task", [{ material_id: "image", is_image: true, size_bytes: 50, relative_path: "image.png" }]);
    state.materialSelections.set("task", { bindings: [{ materialId: "image", note: "原始备注" }], requiredImageIds: ["image"], notes: { image: "原始备注" } });
    state.details.get("task").ui_state.skills = ["original-skill"];
  `);
  const requests = [];
  harness.context.fetch = async (url, options = {}) => {
    if (url.endsWith("/main/runs")) {
      requests.push({ url, options });
      throw new TypeError("Failed to fetch");
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };
  // 允许各场景模拟传输；请求记录始终来自真实 api()/sendMain()。
  const transport = harness.context.fetch;
  harness.context.fetch = async (url, requestOptions = {}) => {
    if (url.endsWith("/main/runs") && options.onMain) {
      requests.push({ url, options: requestOptions });
      return options.onMain(url, requestOptions);
    }
    return transport(url, requestOptions);
  };
  return { ...harness, input, feedback, recovery, send, evaluate, requests };
}

function receipt(payload = { run_id: "run", task_id: "task", message_id: "message", status: "pending", kind: "main" }) {
  return { ok: true, status: 200, json: async () => payload };
}
const identity = request => request.options.headers.get("Idempotency-Key");

function localRepository(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "focus-main-retry-"));
  t.after(() => {
    assert.ok(path.resolve(directory).startsWith(path.resolve(os.tmpdir()) + path.sep));
    fs.rmSync(directory, { recursive: true, force: true });
  });
  return () => new MainRequestStorage(directory);
}

test("跨端口重启从桌面稳定存储恢复原身份、完整请求及未确认状态", async t => {
  const repository = localRepository(t);
  const desktop = port => ({
    location: { origin: `http://127.0.0.1:${port}`, protocol: "http:" },
    focusDesktop: { mainRequestStorage: repository() },
  });
  const accepted = new Map();
  const accept = (_url, options) => {
    const id = options.headers.get("Idempotency-Key");
    if (!accepted.has(id)) accepted.set(id, receipt());
    return accepted.get(id);
  };
  const first = setup({ globals: desktop(1234), onMain: (...args) => {
    accept(...args);
    throw new TypeError("lost receipt");
  } });
  await first.evaluate("sendMain()");
  const restarted = setup({ globals: desktop(5678), onMain: accept });
  assert.equal(restarted.context.localStorage.getItem("focus-main-request:task"), null);
  assert.equal(restarted.evaluate('taskRunOperations.recovery("task")?.failure.uncertain'), true);
  await restarted.evaluate("sendMain()");
  assert.equal(identity(restarted.requests[0]), identity(first.requests[0]));
  assert.equal(restarted.requests[0].options.body, first.requests[0].options.body);
  assert.equal(accepted.size, 1);
  assert.equal(setup({ globals: desktop(9012) }).evaluate('taskRunOperations.recovery("task")'), null);
});

test("投递后进程退出但未保存失败诊断时，仍从磁盘恢复原请求", async t => {
  const repository = localRepository(t);
  let sent;
  const started = new Promise(resolve => { sent = resolve; });
  const globals = () => ({ focusDesktop: { mainRequestStorage: repository() } });
  const first = setup({ globals: globals(), onMain: () => {
    assert.ok(repository().getItem("focus-main-request:task"), "网络投递之前已持久化");
    sent();
    return new Promise(() => {});
  } });
  void first.evaluate("sendMain()");
  await started;
  const restarted = setup({ globals: globals() });
  await restarted.evaluate("sendMain()");
  assert.equal(identity(restarted.requests[0]), identity(first.requests[0]));
  assert.equal(restarted.requests[0].options.body, first.requests[0].options.body);
});

test("桌面仓库写入失败不能退回 localStorage 并投递", async () => {
  const h = setup({ globals: { focusDesktop: { mainRequestStorage: {
    getItem: () => null, setItem: () => { throw new Error("disk full"); }, removeItem() {},
  } } } });
  await h.evaluate("sendMain()");
  assert.equal(h.requests.length, 0);
  assert.equal(h.context.localStorage.getItem("focus-main-request:task"), null);
  assert.match(h.feedback.textContent, /disk full/);
});

test("旧 origin 可见的凭证先迁入磁盘，确认受理后两处均不再恢复", async t => {
  const repository = localRepository(t);
  const first = setup();
  await first.evaluate("sendMain()");
  const key = "focus-main-request:task";
  const globals = () => ({ focusDesktop: { mainRequestStorage: repository() } });
  const migrated = setup({ storage: { [key]: first.context.localStorage.getItem(key) }, globals: globals(), onMain: () => receipt() });
  assert.equal(migrated.evaluate('taskRunOperations.recovery("task").request_id'), identity(first.requests[0]));
  assert.ok(repository().getItem(key));
  assert.equal(migrated.context.localStorage.getItem(key), null);
  await migrated.evaluate("sendMain()");
  assert.equal(identity(migrated.requests[0]), identity(first.requests[0]));
  assert.equal(repository().getItem(key), null);
  assert.equal(setup({ globals: globals() }).evaluate('taskRunOperations.recovery("task")'), null);
});

test("稳定凭证优先于旧 origin 缓存；稳定凭证损坏时阻止投递", async t => {
  const repository = localRepository(t);
  const key = "focus-main-request:task";
  const first = setup({ globals: { focusDesktop: { mainRequestStorage: repository() } } });
  await first.evaluate("sendMain()");
  const saved = JSON.parse(repository().getItem(key));
  const restarted = setup({
    globals: { focusDesktop: { mainRequestStorage: repository() } },
    storage: { [key]: JSON.stringify({ ...saved, request_id: "stale-origin-id" }) },
  });
  await restarted.evaluate("sendMain()");
  assert.equal(identity(restarted.requests[0]), identity(first.requests[0]));
  assert.equal(restarted.context.localStorage.getItem(key), null);
  fs.writeFileSync(repository().file(key), "broken");
  const broken = setup({
    globals: { focusDesktop: { mainRequestStorage: repository() } },
    storage: { [key]: JSON.stringify(saved) },
  });
  assert.throws(() => broken.evaluate("sendMain()"), /Unexpected token/);
  assert.equal(broken.requests.length, 0);
});

test("回执丢失后再次发送沿用身份和完整请求，只受理一次", async () => {
  const accepted = new Map();
  let first = true;
  const h = setup({ onMain: (_url, options) => {
    const id = options.headers.get("Idempotency-Key");
    if (!accepted.has(id)) accepted.set(id, receipt());
    if (first) { first = false; throw new TypeError("Failed to fetch"); }
    return accepted.get(id);
  } });
  await h.evaluate("sendMain()");
  assert.match(h.statusNode.textContent, /尚未确认是否受理/);
  assert.match(h.feedback.textContent, /尚未确认是否受理/);
  assert.match(h.recovery.innerHTML, /按原内容重试/);
  assert.match(h.recovery.innerHTML, /编辑后重新发送/);
  assert.equal(h.input.readOnly, true);
  await h.evaluate("sendMain()");
  assert.equal(h.requests.length, 2);
  assert.equal(identity(h.requests[1]), identity(h.requests[0]));
  assert.equal(h.requests[1].options.body, h.requests[0].options.body);
  assert.equal(accepted.size, 1);
  assert.equal(h.evaluate('taskRunOperations.recovery("task")'), null);
  assert.equal(h.evaluate('state.details.get("task").messages.length'), 1);
  assert.equal(h.evaluate('state.materialHistory.get("task").length'), 1);
  assert.equal(h.evaluate('composerDraft.value("task")'), "");
});

test("再次失败仍保留原消息、材料、必看、权限与身份", async () => {
  const h = setup();
  await h.evaluate("sendMain()");
  await h.evaluate("sendMain()");
  assert.equal(h.requests.length, 2);
  assert.equal(identity(h.requests[1]), identity(h.requests[0]), "回执丢失后再次发送必须沿用原身份");
  assert.equal(h.requests[1].options.body, h.requests[0].options.body, "重试必须使用冻结的原请求");
  assert.match(h.statusNode.textContent, /尚未确认是否受理/);
  const payload = JSON.parse(h.requests[0].options.body);
  assert.deepEqual(payload.material_inputs, [{ material_id: "image", note: "原始备注" }]);
  assert.deepEqual(payload.must_view_material_ids, ["image"]);
  assert.deepEqual(payload.skills, ["original-skill"]);
  assert.equal(h.input.value, "检查这份附件");
  assert.equal(h.evaluate('materialSelection("task").bindings.length'), 1);
});

test("原请求重试不混入变化后的材料、技能、权限、空间目标或执行审批", async () => {
  const h = setup();
  await h.evaluate("sendMain()");
  h.input.value = "下一条消息";
  h.evaluate(`
    composerDraft.claim("task", "下一条消息");
    state.materialSelections.set("task", runMaterialPicker.empty());
    state.details.get("task").ui_state.skills = ["changed-skill"];
    state.details.get("task").ui_state.access_mode = "danger-full-access";
    state.details.get("task").pending_compression = { id: "new-gate" };
    currentSpatialTarget = () => { throw new Error("重试不应读取新空间目标"); };
  `);
  await h.evaluate("sendMain()");
  assert.equal(h.requests.length, 2);
  assert.equal(identity(h.requests[1]), identity(h.requests[0]));
  assert.equal(h.requests[1].options.body, h.requests[0].options.body);
  assert.equal(h.input.value, "下一条消息");
});

test("显式编辑后发送新身份；仅进入编辑仍能重试原请求", async () => {
  const h = setup();
  await h.evaluate("sendMain()");
  h.evaluate("editMainRequest()");
  assert.equal(h.input.readOnly, false);
  h.input.value = "编辑后的内容";
  h.evaluate('composerDraft.claim("task", "编辑后的内容"); state.materialSelections.set("task", runMaterialPicker.empty());');
  await h.evaluate("sendMain(true)");
  assert.equal(identity(h.requests[1]), identity(h.requests[0]));
  assert.equal(h.requests[1].options.body, h.requests[0].options.body);
  await h.evaluate("sendMain()");
  assert.notEqual(identity(h.requests[2]), identity(h.requests[0]));
  assert.equal(JSON.parse(h.requests[2].options.body).message, "编辑后的内容");
  assert.deepEqual(JSON.parse(h.requests[2].options.body).material_inputs, []);
});

test("刷新恢复冻结请求，当前详情或材料变化不改变重试载荷", async () => {
  const h = setup();
  await h.evaluate("sendMain()");
  const key = "focus-main-request:task";
  const stored = h.context.localStorage.getItem(key);
  assert.ok(stored);
  const restarted = setup({ storage: { [key]: stored } });
  restarted.evaluate('state.materials.set("task", []); state.details.get("task").ui_state.skills = [];');
  restarted.input.value = "";
  await restarted.evaluate("sendMain()");
  assert.equal(identity(restarted.requests[0]), identity(h.requests[0]));
  assert.equal(restarted.requests[0].options.body, h.requests[0].options.body);
});

test("重载后详情加载恢复原文及附件，后续刷新保留用户新草稿", async () => {
  const h = setup();
  await h.evaluate("sendMain()");
  const key = "focus-main-request:task";
  const restarted = setup({ storage: { [key]: h.context.localStorage.getItem(key) } });
  restarted.evaluate(`
    composerDraft.release("task");
    state.details.set("task", { messages: [], ui_state: {} });
    state.materialSelections.set("task", runMaterialPicker.empty());
    api = async path => {
      if (path.endsWith("/materials")) return [{ material_id: "image", is_image: true, size_bytes: 50 }];
      if (path.endsWith("/skills")) return { skills: [] };
      if (path.endsWith("/agents") || path.endsWith("/material-groups") || path.endsWith("/material-history")) return [];
      return { messages: [], ui_state: { input: "早于发送的旧草稿" } };
    };
  `);
  await restarted.evaluate('hydrateActive("task")');
  assert.equal(restarted.evaluate('state.details.get("task").ui_state.input'), "检查这份附件");
  assert.equal(restarted.evaluate('materialSelection("task").bindings[0].materialId'), "image");
  assert.match(restarted.document.querySelector("#app").innerHTML, /data-action="retry-main"/);
  assert.equal(restarted.input.readOnly, true);
  restarted.evaluate('editMainRequest(); composerDraft.claim("task", "新的内容"); state.materialSelections.set("task", runMaterialPicker.empty());');
  await restarted.evaluate('hydrateActive("task")');
  assert.equal(restarted.evaluate('composerDraft.value("task")'), "新的内容");
  assert.equal(restarted.evaluate('materialSelection("task").bindings.length'), 0);
});

test("连点只投递一次；另一个任务可独立发送且迟到回执保留新草稿", async () => {
  let complete;
  const h = setup({ onMain: url => url.includes("/task/")
    ? new Promise(resolve => { complete = resolve; }) : receipt({ run_id: "run-b", task_id: "other", message_id: "message-b", status: "pending" }) });
  const first = h.evaluate("sendMain()");
  await h.evaluate("sendMain()");
  assert.equal(h.requests.length, 1);
  h.input.value = "另一个任务";
  h.evaluate(`
    composerDraft.claim("task", "原任务新草稿");
    state.tasks.push({ task_id: "other", workspace_id: "workspace", workspace_path: "C:/workspace" });
    state.details.set("other", { messages: [], ui_state: { input: "另一个任务" } });
    state.activeTaskId = "other";
  `);
  await h.evaluate("sendMain()");
  complete(receipt());
  await first;
  assert.equal(h.requests.length, 2);
  assert.notEqual(identity(h.requests[0]), identity(h.requests[1]));
  assert.equal(h.evaluate('composerDraft.value("task")'), "原任务新草稿");
  assert.equal(h.evaluate('state.details.get("task").active_run.run_id'), "run");
  assert.equal(h.evaluate('state.details.get("other").active_run.run_id'), "run-b");
});

test("原身份的直接消息回执只确认受理，保留期间写入的新草稿和材料", async () => {
  let first = true;
  const h = setup({ onMain: () => {
    if (first) { first = false; throw new TypeError("offline"); }
    return receipt({ intent_kind: "direct_message", intent_id: "intent", run_id: null, status: "pending" });
  } });
  await h.evaluate("sendMain()");
  h.input.value = "新草稿";
  h.evaluate('composerDraft.claim("task", "新草稿"); state.materialSelections.set("task", runMaterialPicker.empty());');
  await h.evaluate("sendMain(true)");
  assert.equal(h.evaluate('taskRunOperations.recovery("task")'), null);
  assert.equal(h.evaluate('taskRunOperations.get("task").active_run'), null);
  assert.equal(h.evaluate('composerDraft.value("task")'), "新草稿");
  assert.equal(h.input.value, "新草稿");
  assert.equal(h.evaluate('state.materialHistory.has("task")'), false);
});

test("网关错误、损坏或空回执均不放弃原身份", async () => {
  for (const response of [
    { ok: false, status: 503, json: async () => ({ detail: "gateway lost receipt" }) },
    { ok: true, status: 200, json: async () => { throw new SyntaxError("broken JSON"); } },
    receipt(null),
  ]) {
    const h = setup({ onMain: () => response });
    await h.evaluate("sendMain()");
    await h.evaluate("sendMain()");
    assert.equal(identity(h.requests[1]), identity(h.requests[0]));
    assert.match(h.feedback.textContent, /尚未确认是否受理/);
  }
});

test("明确拒绝显示未受理，修改请求仍需显式进入编辑", async () => {
  const h = setup({ onMain: () => ({ ok: false, status: 422, json: async () => ({ detail: "invalid attachment" }) }) });
  await h.evaluate("sendMain()");
  assert.match(h.feedback.textContent, /发送未受理/);
  assert.equal(h.input.readOnly, true);
  await h.evaluate("sendMain()");
  assert.equal(identity(h.requests[1]), identity(h.requests[0]));
});

test("已确认受理后的本地刷新错误不会变成未确认或再次投递", async () => {
  const h = setup({ onMain: () => receipt() });
  h.evaluate('listenToRun = () => { throw new Error("SSE failed"); };');
  await h.evaluate("sendMain()");
  assert.match(h.statusNode.textContent, /消息已受理，但本地状态更新失败/);
  assert.equal(h.evaluate('taskRunOperations.recovery("task")'), null);
  assert.equal(h.context.localStorage.getItem("focus-main-request:task"), null);
});

test("冻结请求保存失败时不向后端投递", async () => {
  const h = setup();
  h.context.localStorage.setItem = () => { throw new Error("disk full"); };
  await h.evaluate("sendMain()");
  assert.equal(h.requests.length, 0);
  assert.equal(h.input.value, "检查这份附件");
  assert.match(h.feedback.textContent, /disk full/);
});
