const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const { createElement } = require("../helpers/test-dom.cjs");

function surface(locale = "en-US") {
  const document = { documentElement: { dataset: {} }, dispatchEvent() {}, querySelectorAll: () => [] };
  const context = vm.createContext({ document, localStorage: { getItem: () => locale, setItem() {} }, CustomEvent: class {} });
  context.window = context;
  for (const file of ["../../src/renderer/shared/i18n/i18n-catalog.js", "../../src/renderer/shared/i18n/i18n.js", "../../src/renderer/features/plugins/plugin-view.js", "../../src/renderer/features/memory/memory.js", "../../src/renderer/features/settings/session-controls.js", "../../src/renderer/features/patrol/workspace/workspace-patrol-feedback.js", "../../src/renderer/features/patrol/workspace/patrol-input-references.js", "../../src/renderer/features/patrol/workspace/workspace-patrol-view.js", "../../src/renderer/features/loop/loop-expansion-budget.js", "../../src/renderer/features/loop/loop-facts-view.js", "../../src/renderer/features/loop/loop-grant-view.js", "../../src/renderer/features/patrol/workspace/workspace-patrol-budget.js"]) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, file), "utf8"), context, { filename: file });
  }
  return context;
}

for (const file of ["../../src/renderer/index.html", "../../src/renderer/desktop-patrol.html", "../../src/renderer/splash.html"]) test(`English ${file} translates text, titles and accessible labels`, () => {
  const context = surface();
  const root = createElement("div");
  root.innerHTML = fs.readFileSync(path.join(__dirname, file), "utf8").replace(/<!--[\s\S]*?-->/g, "");
  context.FocusI18n.apply(root);
  const untranslated = [];
  function visit(node) {
    if (["script", "style"].includes(node.tagName)) return;
    if (node.nodeType === 3 && /\p{Script=Han}/u.test(node.textContent)) untranslated.push(node.textContent.trim());
    for (const name of ["aria-label", "title", "placeholder"]) {
      if (/\p{Script=Han}/u.test(node.getAttribute?.(name) || "")) untranslated.push(node.getAttribute(name));
    }
    for (const child of node.childNodes || []) visit(child);
  }
  visit(root);
  assert.deepEqual(untranslated, []);
});

test("English catalog has a translation for every source message", () => {
  const catalog = require("../../src/renderer/shared/i18n/i18n-catalog.js");
  for (const [source, english] of Object.entries(catalog)) {
    assert.equal(typeof english, "string", source);
    assert.doesNotMatch(english, /\p{Script=Han}/u, source);
  }
});

test("source templates translate only literals and preserve interpolation values", () => {
  const c = surface();
  const value = "刷新 <script>用户内容</script>";
  assert.equal(c.FocusI18n.text`文件 / 产物${value}`, `Files / Outputs${value}`);
  assert.equal(c.FocusI18n.text`上下文约${95}% 已用`, "Context about 95% used");
  c.FocusI18n.setLocale("zh-CN");
  assert.equal(c.FocusI18n.text`上下文约${95}% 已用`, "上下文约95% 已用");
});

test("English dynamic views contain no built-in Chinese text", () => {
  const c = surface();
  const escape = value => String(value ?? "");
  const html = [
    c.FocusPluginView.render([], {}, [], {}),
    c.FocusMemoryView.render([], {}),
    c.FocusSessionControls.render({ models: [], default_model_name: "" }, {}, null, escape),
    c.FocusWorkspacePatrolView.skeleton({ workspace_id: "w", display_name: "Workspace" }),
  ].join("\n");
  assert.doesNotMatch(html, /\p{Script=Han}/u);
});

test("memory category emptiness and search recovery follow the interface language", () => {
  const c = surface();
  const memory = { memory_id: "note", title: "Note", content: "Content" };
  const strategy = { memory_id: "strategy", category: "evolution_strategy", title: "Method", strategy: { applicability: "When needed" } };
  for (const category of ["memory", "evolution_strategy"]) {
    const items = category === "memory" ? [strategy] : [memory];
    const empty = c.FocusMemoryView.render(items, { category });
    const noMatches = c.FocusMemoryView.render([memory, strategy], { category, query: "missing" });
    assert.doesNotMatch(empty + noMatches, /\p{Script=Han}/u);
    assert.match(empty, category === "memory" ? /No memories yet/ : /No work strategies yet/);
    assert.match(noMatches, /Clear search/);
  }
});

test("strategy methods, publishing, capacity and rehearsal copy are translated", () => {
  const c = surface();
  const strategy = { applicability: "When needed", evolution: "Proceed", inheritance: "Evidence" };
  const metadata = { id: "test/method", name: "Method", version: "1.0.0", description: "Description" };
  const rehearsal_result = { applicable: { adopted: true, reason: "Reason", organization: "Plan", inheritance: "Evidence", missing_evidence: [], stop_when: "Stop" } };
  const editor = c.FocusMemoryView.render([], { composing: true, draft: { title: "Method", category: "evolution_strategy", strategy, rehearsal_result } });
  const importing = c.FocusMemoryView.render([], { strategyImport: { markdown: "Source", preview: {
    metadata, strategy, existing: { strategy, strategy_package: metadata, locally_modified: true },
    capacity: [{ fits: false, input_bound: 50, previous_input_bound: 60, strategy_input_limit: 40, model_name: "Model" }],
  } } });
  assert.doesNotMatch(editor + importing, /\p{Script=Han}/u);
  assert.match(editor, /When to apply/);
  assert.match(editor, /Rehearsal results \(simulation\)/);
});

test("context body and JSON editors translate labels while preserving message fields", () => {
  const c = surface();
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../../src/renderer/features/context/context-editor.js"), "utf8"), c);
  const messages = [{ role: "assistant", content: [{ type: "text", text: "用户正文" }], tool_call_id: "call-1" }];
  for (const mode of ["body", "json"]) {
    const html = c.FocusContextEditor.renderMessages(messages, ["a"], "a", false, mode);
    assert.doesNotMatch(html.replaceAll("用户正文", ""), /\p{Script=Han}/u);
    assert.match(html, /Create duplicate/);
    assert.match(html, /用户正文/);
  }
});

test("language changes refresh generated labels and preserve user content", () => {
  const c = surface("zh-CN");
  const render = () => c.FocusPluginView.render([{ name: "用户插件", version: "1", status: "active", requires: [] }], {}, [], {});
  assert.match(render(), /已注册/);
  c.FocusI18n.setLocale("en-US");
  const english = render();
  assert.match(english, /用户插件/);
  assert.doesNotMatch(english.replaceAll("用户插件", ""), /\p{Script=Han}/u);
  c.FocusI18n.setLocale("zh-CN");
  assert.match(render(), /已注册/);
});

test("budget and fact labels follow repeated language switches after modules have loaded", () => {
  const c = surface("zh-CN");
  const render = () => [
    c.FocusLoopExpansionBudget.renderLoopInputs(),
    c.FocusLoopGrantView.grantControls({ status: "running", grant: { budgets: {} } }),
    c.FocusLoopFactsView.statusOptions.map(row => row[1]).join(" "),
    c.FocusLoopFactsView.outcomeOptions.map(row => row[1]).join(" "),
    c.FocusWorkspacePatrolBudget.markup(),
  ].join("\n");
  assert.match(render(), /最大轮次/);
  c.FocusI18n.setLocale("en-US");
  assert.doesNotMatch(render(), /\p{Script=Han}/u);
  c.FocusI18n.setLocale("zh-CN");
  assert.match(render(), /最大轮次/);
});

test("native chrome follows the saved language and leaves unknown content intact", () => {
  const { createNativeI18n } = require("../../src/main/native-i18n.cjs");
  let locale = "zh-CN";
  const text = createNativeI18n(() => locale);
  assert.equal(text("退出 Focus"), "退出 Focus");
  locale = "en-US";
  assert.equal(text("退出 Focus"), "Quit Focus");
  assert.equal(text("我的项目"), "我的项目");
});

test("Patrol feedback uses stable bilingual keys without the source catalog", async () => {
  const c = surface("zh-CN");
  c.FocusI18nCatalog = {};
  const feedback = c.FocusWorkspacePatrolFeedback;
  const reader = feedback.createReader({ conversation: async () => { throw new Error("offline"); } });
  const cancellationStates = ["cancel_requested", "stopped", "already_terminal", "settled", "unknown", "future_state"];
  const result = {
    disposition: "decision", explanation: "User explanation", mission_revision: 3,
    decision_id: "decision-1", round_id: "round-2", observation_id: "observation-3", observation_generation: 4,
    applied_at: "2026-11-10T04:00:00Z",
    changes: ["supplement", "revise", "remove"].map((operation, i) => ({ operation, section: ["outcome", "boundary", "completion_check"][i], quote: "User quote" })),
    related_cancellations: cancellationStates.map((state, i) => ({ state, target_kind: ["run", "directive", "worker"][i % 3], target_id: `target-${i}`, reason: "User reason" })),
  };
  const loop = { interaction_mode: "workspace_patrol", status: "waiting_user", waiting_reason: "awaiting_input",
    current_round_id: "round-2", goal_revision: 3,
    final_result: { round_id: "round-2", goal_revision: 3,
      verification: { verification_id: "verified", conclusion: "satisfied", criteria: [{ check_id: "check", status: "unknown", explanation: "Unverified" }] },
      final_path: [{ context_id: "context-1", revision_id: "revision-2" }] } };
  const before = JSON.stringify({ result, loop });
  for (const locale of ["zh-CN", "en-US", "zh-CN", "en-US"]) {
    c.FocusI18n.setLocale(locale);
    const html = [
      ...["discussion", "decision", "reference", "deferred", "future_disposition"].map(disposition => feedback.inputResult({ status: "accepted", processing_result: { ...result, disposition } })),
      ...[undefined, "pending", "observed", "addressed", "superseded"].map(processing_state => feedback.inputResult({ status: "accepted", processing_state })),
      feedback.delivery({ loop: { state: loop } }),
      feedback.delivery({ loop: { state: { ...loop, current_round_id: "next-round", final_result: { ...loop.final_result,
        verification: { verification_id: "verified", conclusion: "satisfied", criteria: [{ check_id: "check", status: "satisfied", explanation: "Passed" }] } } } } }),
      ...["running", "waiting_user", "paused", "stopped", "failed", "completed"].map(status => feedback.status({ loop: { state: { status } } }, { status: "live" }, { rows: [] }).text),
      ...["failed", "sending", "accepted"].map(status => feedback.status(null, { status: "live" }, { rows: [{ status }] }).text),
      feedback.status(null, { status: "reconnecting" }, { rows: [] }).text,
      feedback.status(null, { status: "live" }, { rows: [] }).text,
      feedback.status({ loop: { state: loop } }, { status: "live" }, { rows: [] }).text,
      ...["stopped", "failed"].flatMap(status => [[], [{ status: "accepted", processing_state: "pending" }]].map(rows => JSON.stringify(feedback.recovery({ loop: { state: { status } } }, { rows })))),
      reader.entries({ contexts: { c: { entity_id: "c", state: { current_revision_id: "r" } } } })[0].title,
      (await reader.read("loop", { context_id: "c", revision_id: "r" })).error,
    ].join("\n");
    assert.doesNotMatch(html, /patrol\.(?:feedback|delivery|status|recovery|answer)\./, "no missing-key fallback");
    if (locale === "en-US") assert.doesNotMatch(html, /\p{Script=Han}/u, "all generated feedback and accessible names must be English");
    else assert.match(html, /已纳入决策/);
    const host = createElement("div");
    host.innerHTML = feedback.delivery({ loop: { state: loop } });
    const action = host.querySelector('[data-patrol-delivery-context="context-1"]');
    assert.equal(action.dataset.revisionId, "revision-2");
    assert.equal(host.querySelector("nav").getAttribute("aria-label"), c.FocusI18n.t("patrol.delivery.view"));
    assert.ok(host.querySelector("[data-patrol-files-open]"));
  }
  assert.equal(JSON.stringify({ result, loop }), before, "language changes preserve domain data");
});

test("translated Patrol templates preserve and escape user text and action identities", () => {
  const c = surface();
  c.FocusI18nCatalog = {};
  const userText = '用户原文 <img src=x onerror="bad()">';
  const escaped = '用户原文 &lt;img src=x onerror=&quot;bad()&quot;&gt;';
  const feedback = c.FocusWorkspacePatrolFeedback;
  const html = feedback.inputResult({ status: "accepted", processing_result: {
    disposition: "decision", explanation: userText, rationale: userText,
    changes: [{ operation: "revise", section: "boundary", quote: userText }],
    related_cancellations: [{ state: "cancel_requested", target_kind: "run", target_id: userText, reason: userText }],
  } }) + feedback.delivery({ loop: { state: { interaction_mode: "workspace_patrol", final_result: {
    summary: userText, verification: { verification_id: "v", conclusion: "satisfied" },
    final_path: [{ context_id: userText, revision_id: userText, title: userText }],
  } } } });
  assert.ok(html.includes(escaped));
  assert.ok(html.includes(`data-patrol-delivery-context="${escaped}" data-revision-id="${escaped}"`));
  assert.ok(html.includes(`Revise Execution boundary: ${escaped}`), "translated interpolation preserves the whole user quote");
  assert.doesNotMatch(html, /<img|<script|<svg|<iframe/);
  assert.doesNotMatch(html.replaceAll(escaped, ""), /\p{Script=Han}/u);
});
