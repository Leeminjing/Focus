/* 验证实际策略页面渲染、导入预览/替换、异步隔离及作者工具；使用现有 app VM 合同。 */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { test } = require("node:test");
const { createAppHarness, readAppSource } = require("../helpers/test-helper.cjs");
const Memory = require("../../src/renderer/features/memory/memory.js");
const Observation = require("../../src/renderer/features/patrol/workspace/observation-view.js");

const strategy = { applicability: "关键假设未验证", evolution: "先验证再实施", inheritance: "目标和证据" };
const metadata = { schema: "focus.strategy/v1", id: "tests/validate", name: "先验证", version: "1.0.0", description: "减少返工" };
const installed = { memory_id: "memory-1", title: metadata.name, category: "evolution_strategy", strategy, enabled: true, strategy_package: metadata, installation_hash: "a".repeat(64), locally_modified: true };

function harness(extra = {}) {
  const h = createAppHarness({ statusNode: "record", globals: { strategy, metadata, installed, ...extra } });
  for (const filename of ["../../src/renderer/features/patrol/workspace/workspace-patrol-inputs.js", "../../src/renderer/features/patrol/workspace/workspace-patrol-intake.js"]) vm.runInContext(fs.readFileSync(require.resolve(`./${filename}`), "utf8"), h.context);
  vm.runInContext(fs.readFileSync(require.resolve("../../src/renderer/features/memory/memory.js"), "utf8"), h.context);
  new vm.Script(readAppSource().replace(/runUiAction\(async \(\) => \{ await bootstrap\(\); if \(desktopPatrol\) await desktopPatrol.ready\(\); \}\);\s*$/, "")).runInContext(h.context);
  const run = code => vm.runInContext(code, h.context);
  run('renderMemory = () => {}; render = () => {}; state.view = "memory";');
  return { ...h, run };
}

test("strategy category has its own copy and creates strategies when empty", () => {
  const html = Memory.render([], { category: "evolution_strategy" });
  assert.match(html, /Strategy · 工作策略/);
  assert.match(html, /搜索方法与适用场景/);
  assert.match(html, /data-action="import-strategy"/);
  assert.doesNotMatch(html, /data-action="new-memory"/);
  const detail = Memory.render([installed], { category: "evolution_strategy", selectedId: installed.memory_id });
  assert.match(detail, /本地已修改/);
  assert.match(detail, /data-action="export-strategy"/);
  assert.match(detail, /tests\/validate/);
});

test("new strategy editor renders with an explicitly empty authoring state", () => {
  const html = Memory.render([], { composing: true, draft: { category: "evolution_strategy", title: "Test", strategy }, authoring: null });
  assert.match(html, /data-memory-field="package:id"/);
  assert.match(html, /用三个场景预演方法/);
});

test("failed save retains the generated stable package identity for retry", async () => {
  const requests = [];
  const { run } = harness({ requests });
  run('openMemoryCompose(false); state.memory.draft = {...state.memory.draft, category: "evolution_strategy", title: "Test", strategy}; api = async (path, options) => { requests.push(JSON.parse(options.body)); throw new Error("network"); };');
  await run("saveStrategy()");
  await run("saveStrategy()");
  assert.equal(requests.length, 2);
  assert.equal(requests[0].strategy_package.id, requests[1].strategy_package.id);
  assert.equal(run("state.memory.strategySaving"), false);
});

test("import preview compares local edits with incoming sections and escapes documents", () => {
  const html = Memory.render([], { strategyImport: { markdown: "<script>bad()</script>", enabled: false,
    preview: { metadata, strategy, existing: installed } } });
  assert.match(html, /当前安装/);
  assert.match(html, /待导入/);
  assert.match(html, /另存为新策略并编辑/);
  assert.match(html, /包含本地修改/);
  assert.doesNotMatch(html, /<script>bad/);
  assert.match(html, /&lt;script&gt;/);
});

test("import preview shows collection capacity and safely attributes strategy usage", () => {
  const html = Memory.render([], { strategyImport: { markdown: "document", enabled: true,
    preview: { metadata, strategy, capacity: [{ fits: false, workspace_id: "workspace", model_name: "model",
      input_bound: 70000, previous_input_bound: 30000, strategy_input_limit: 40000,
      strategies: [{ title: "<Large strategy>", package_id: "tests/large", input_bound: 35000 }] }] } } });
  assert.match(html, /策略集合容量超限/);
  assert.match(html, /70000 \/ 40000/);
  assert.match(html, /&lt;Large strategy&gt;/);
  assert.match(html, /tests\/large/);
  assert.match(html, /data-action="install-strategy" disabled/);
});

test("preview sends the selected disabled state to capacity admission", async () => {
  const requests = [];
  const { run } = harness({ requests });
  run('openStrategyImport(); state.memory.strategyImport.markdown = "document"; state.memory.strategyImport.enabled = false; api = async (path, options) => { requests.push(JSON.parse(options.body)); return {metadata, strategy}; };');
  await run("previewStrategyImport()");
  assert.equal(requests[0].enabled, false);
});

test("capacity preview keeps reductions available for an existing oversized set", () => {
  const html = Memory.render([], { strategyImport: { markdown: "document", preview: { metadata, strategy,
    capacity: [{ fits: false, input_bound: 50000, previous_input_bound: 60000, strategy_input_limit: 40000, strategies: [] }] } } });
  assert.doesNotMatch(html, /data-action="install-strategy" disabled/);
});

test("changing import content invalidates the preview before any installation", async () => {
  const { run } = harness();
  run('openStrategyImport(); state.memory.strategyImport.preview = { metadata, strategy }; api = async () => { throw new Error("must not install"); }; syncStrategyImportField("markdown", "changed");');
  assert.equal(run("state.memory.strategyImport.preview"), null);
  await run("installStrategyImport()");
});

test("reopening import cannot receive a previous preview response", async () => {
  let resolvePreview;
  const pending = new Promise(resolve => { resolvePreview = resolve; });
  const { run } = harness({ previewResponse: pending });
  run('api = () => previewResponse; openStrategyImport(); state.memory.strategyImport.markdown = "old";');
  const request = run("previewStrategyImport()");
  run('openStrategyImport(); state.memory.strategyImport.markdown = "new";');
  resolvePreview({ metadata, strategy });
  await request;
  assert.equal(run("state.memory.strategyImport.markdown"), "new");
  assert.equal(run("state.memory.strategyImport.preview"), null);
});

test("replacement sends the exact preview identity and loses stale preview on conflict", async () => {
  const requests = [];
  const { run } = harness({ requests });
  run(`api = async (path, options) => { requests.push({ path, body: JSON.parse(options.body) }); throw new Error("preview changed"); };
    openStrategyImport(); state.memory.strategyImport.markdown = "incoming";
    state.memory.strategyImport.preview = { metadata, strategy, existing: installed };`);
  await run("installStrategyImport()");
  assert.equal(requests.length, 1);
  assert.equal(requests[0].body.replace_memory_id, installed.memory_id);
  assert.equal(requests[0].body.expected_hash, installed.installation_hash);
  assert.equal(run("state.memory.strategyImport.preview"), null);
  assert.match(run("state.memory.strategyImport.error"), /preview changed/);
});

test("forking an import creates a new package identity and keeps its body", () => {
  const { run } = harness();
  run('openStrategyImport(); state.memory.strategyImport.preview = { metadata, strategy, existing: installed }; forkImportedStrategy();');
  assert.equal(run("state.memory.draft.memory_id"), undefined);
  assert.match(run("state.memory.draft.strategy_package.id"), /^local\//);
  assert.equal(run("state.memory.draft.strategy.evolution"), strategy.evolution);
  assert.equal(run("state.memory.strategyImport"), null);
});

test("editing package fields does not mix metadata with the three section body", () => {
  const { run } = harness();
  run('state.memory.memories = [installed]; state.memory.selectedId = installed.memory_id; openMemoryCompose(true); syncMemoryField("package:description", "new description");');
  assert.equal(run("state.memory.draft.strategy_package.description"), "new description");
  assert.equal(run("state.memory.draft.strategy.description"), undefined);
});

test("authoring generates an unsaved editable strategy from selected text", async () => {
  const requests = [];
  const { run } = harness({ requests });
  run(`openMemoryCompose(false); state.memory.draft.content = "work experience";
    api = async (path, options) => { requests.push({path, body: JSON.parse(options.body)}); return { title: "Generated", strategy }; };`);
  await run("generateStrategyDraft()");
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, "/desktop/api/strategies/draft");
  assert.equal(requests[0].body.selection.sources[0].text, "work experience");
  assert.equal(run("state.memory.draft.title"), "Generated");
  assert.equal(run("state.memory.draft.category"), "evolution_strategy");
  assert.equal(run("state.memory.draft.memory_id"), undefined);
});

test("an authoring response cannot replace a newly opened draft", async () => {
  let resolveDraft;
  const response = new Promise(resolve => { resolveDraft = resolve; });
  const { run } = harness({ response });
  run('openMemoryCompose(false); state.memory.draft.content = "experience"; api = () => response;');
  const request = run("generateStrategyDraft()");
  run('openMemoryCompose(false); state.memory.draft.title = "New draft";');
  resolveDraft({ title: "Old response", strategy });
  await request;
  assert.equal(run("state.memory.draft.title"), "New draft");
});

test("rewriting an installed strategy preserves its identity and workspace", async () => {
  const { run } = harness();
  run('state.memory.memories = [{...installed, workspace_id: "workspace-test"}]; state.memory.selectedId = installed.memory_id; openMemoryCompose(true); state.memory.draft.strategy_source = "experience"; api = async () => ({title: "Revised", strategy});');
  await run("generateStrategyDraft()");
  assert.equal(run("state.memory.draft.memory_id"), installed.memory_id);
  assert.equal(run("state.memory.draft.strategy_package.id"), metadata.id);
  assert.equal(run("state.memory.draft.workspace_id"), "workspace-test");
  assert.equal(run("state.memory.strategyAuthoring"), null);
});

test("editing a strategy invalidates previous rehearsal output", () => {
  const { run } = harness();
  run('openMemoryCompose(false); state.memory.draft.rehearsal_result = {applicable: { adopted: true }}; syncMemoryField("strategy:evolution", "updated method");');
  assert.equal(run("state.memory.draft.rehearsal_result"), null);
});

test("adoptions display frozen versions and safely render the reason", () => {
  const html = Observation.rounds({ rounds: { r: { state: { number: 1, status: "settled", strategy_adoptions: [
    { title: "<Method>", reason: "<feedback>", package: { id: "tests/validate", version: "1.0.0" }, action_indexes: [0], content_hash: "a".repeat(64) },
  ] } } } });
  assert.match(html, /已采用「&lt;Method&gt;」/);
  assert.match(html, /&lt;feedback&gt;/);
  assert.match(html, /v1.0.0/);
  assert.match(html, /动作 1/);
  assert.doesNotMatch(html, /<Method>/);
});
