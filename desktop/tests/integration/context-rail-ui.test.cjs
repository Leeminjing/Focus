/* The rail uses full source relationships for membership and identity, while
 * technical diagnostics stay inside a native disclosure. No backend required. */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { createAppHarness, readAppSource } = require("../helpers/test-helper.cjs");
const { createElement } = require("../helpers/test-dom.cjs");

function fixture() {
  const root = createElement("div");
  root.rootMarker = true;
  const h = createAppHarness({ selectors: { "#inspectorContent": root, "#appInspector": createElement("aside") } });
  const evaluate = source => h.vm.runInContext(source, h.context);
  for (const file of ["../../src/renderer/shared/i18n/i18n-catalog.js", "../../src/renderer/shared/i18n/i18n.js"]) {
    evaluate(fs.readFileSync(require.resolve(file), "utf8"));
  }
  evaluate(readAppSource());
  evaluate(`
    state.tasks = [
      { task_id: "root-full-id", title: "项目目标", workspace_id: "ws" },
      { task_id: "child-full-id", title: "分析方案", workspace_id: "ws" },
      { task_id: "merged-full-id", title: "汇总方案", workspace_id: "ws" },
      { task_id: "external-full-id", title: "跨分支来源", workspace_id: "ws" },
      { task_id: "blocked-full-id", title: "分析方案", workspace_id: "ws" },
      { task_id: "unrelated", title: "无关会话", workspace_id: "ws" },
      { task_id: "foreign", title: "其他工作区", workspace_id: "other" }
    ];
    state.activeTaskId = "child-full-id";
    state.contextTrees.set("ws", [
      { context_id: "root-full-id", title: "项目目标", depth: 0, parents: [], projection_status: "root" },
      { context_id: "child-full-id", title: "分析方案", depth: 1, parents: [{ context_id: "root-full-id" }], projection_status: "valid", editable: true, cache_hit_rate: 0.25, managed_status: "following", managed_health: "idle" },
      { context_id: "external-full-id", title: "跨分支来源", depth: 0, parents: [], projection_status: "root" },
      { context_id: "deleted-full-id", title: "已删除的参考", lifecycle: "deleted", depth: 0, parents: [] },
      { context_id: "merged-full-id", depth: 2, parents: [{ context_id: "child-full-id" }, { context_id: "external-full-id" }, { context_id: "deleted-full-id" }], projection_status: "valid", managed_status: "following", managed_health: "degraded" },
      { context_id: "blocked-full-id", depth: 1, parents: [{ context_id: "root-full-id" }], projection_status: "approval_required" },
      { context_id: "unrelated", depth: 0, parents: [] },
      { context_id: "unreferenced-deleted", lifecycle: "deleted", parents: [{ context_id: "root-full-id" }] }
    ]);
  `);
  const render = () => { root.innerHTML = evaluate("renderContextRail(activeTask())"); return root; };
  render();
  return { ...h, evaluate, root, render };
}

test("main cards show names and actionable exceptions; diagnostics are collapsed", () => {
  const { root } = fixture();
  const current = root.querySelector('.context-rail-card[aria-current="true"]');
  assert.equal(current.dataset.taskId, "child-full-id");
  assert.equal(current.textContent.trim(), "分析方案");
  const row = current.closest(".context-rail-item");
  const details = row.querySelector("details");
  assert.equal(details.open, false);
  assert.match(details.textContent, /child-full-id/);
  assert.match(details.textContent, /25%/);
  assert.match(details.textContent, /持续跟踪/);
  assert.match(details.textContent, /idle/);
  assert.doesNotMatch(current.textContent, /派生 Context|child-full-id|25%|idle|受管/);
  assert.equal(row.querySelector('[data-action="edit-context-definition"]'), null);
  assert.equal(root.querySelector('[data-action="derive-context"]'), null);
  assert.equal(row.getAttribute("data-context-depth"), "1");
  assert.match(root.querySelector('[data-context-id="merged-full-id"] .context-rail-alert').textContent, /更新失败/);
});

test("all source identities survive cross-root merges, including tree-only deleted sources", () => {
  const { root, evaluate } = fixture();
  const merged = root.querySelector('.context-rail-item[data-context-id="merged-full-id"]');
  // The lightweight DOM preserves escaped source attributes; browsers decode them.
  assert.deepEqual(JSON.parse(merged.getAttribute("data-parent-context-ids").replaceAll("&quot;", '"')), ["child-full-id", "external-full-id", "deleted-full-id"]);
  assert.deepEqual(merged.querySelectorAll(".context-rail-source").map(node => node.dataset.taskId), ["child-full-id", "external-full-id"]);
  const relations = merged.querySelector(".context-rail-parents");
  assert.equal(merged.querySelector(".context-rail-card").getAttribute("aria-describedby"), relations.getAttribute("id"));
  assert.match(relations.textContent, /已删除的参考/);
  const tombstone = root.querySelector('.context-rail-item[data-context-id="deleted-full-id"]');
  assert.ok(tombstone, "secondary-only source absent from active tasks remains visible");
  assert.equal(tombstone.querySelector('[data-action="context-rail-card"]'), null);
  assert.match(tombstone.textContent, /已删除/);
  assert.equal(evaluate('contextHasDescendants("deleted-full-id", state.contextTrees.get("ws"))'), true);
  assert.equal(root.querySelector('[data-context-id="unreferenced-deleted"]'), null);
  assert.equal(root.querySelector('[data-task-id="unrelated"]'), null);
  assert.equal(root.querySelector('[data-task-id="foreign"]'), null);
  assert.equal(root.querySelectorAll('.context-rail-item[data-context-id="merged-full-id"]').length, 1);
});

test("blocked contexts stay read-only and source navigation uses the object ID", async () => {
  const { root, context, evaluate } = fixture();
  const blocked = root.querySelector('.context-rail-item[data-context-id="blocked-full-id"]');
  assert.match(blocked.querySelector(".context-rail-card").textContent, /受阻/);
  assert.ok(blocked.querySelector(".is-blocked"));
  assert.equal(blocked.querySelector('[data-action="resume-context-decision"]'), null);
  context.__button = root.querySelector('.context-rail-item[data-context-id="merged-full-id"] [data-task-id="external-full-id"]');
  evaluate("switchTask = async id => { globalThis.__selected = id; }");
  await evaluate("handleDocumentClick({ target: __button })");
  assert.equal(context.__selected, "external-full-id");
});

test("diagnostic disclosure survives refresh and can be closed again", () => {
  const { root, dispatch, render } = fixture();
  const selector = '.context-rail-diagnostics[data-context-id="child-full-id"]';
  let details = root.querySelector(selector);
  details.open = true;
  dispatch("toggle", { target: details });
  render();
  details = root.querySelector(selector);
  assert.equal(details.open, true);
  details.open = false;
  dispatch("toggle", { target: details });
  render();
  assert.equal(root.querySelector(selector).open, false);
});

test("an immediate inspector refresh preserves native disclosure before toggle is delivered", () => {
  const { root, evaluate } = fixture();
  const selector = '.context-rail-diagnostics[data-context-id="child-full-id"]';
  root.querySelector(selector).open = true;
  evaluate('state.view = "focus"; state.inspector.open = true; state.inspector.tab = "context"; renderInspector()');
  assert.equal(root.querySelector(selector).open, true);
  root.querySelector(selector).open = false;
  evaluate('renderInspector()');
  assert.equal(root.querySelector(selector).open, false);
});

test("English rail translates UI while preserving names, identities and raw diagnostic values", () => {
  const { root, evaluate, render } = fixture();
  evaluate('FocusI18n.setLocale("en-US")');
  render();
  const details = root.querySelector('.context-rail-diagnostics[data-context-id="child-full-id"]');
  assert.match(details.textContent, /Technical details/);
  assert.match(details.textContent, /Execution projection/);
  assert.match(details.textContent, /Continuous tracking/);
  assert.doesNotMatch(details.textContent, /\p{Script=Han}/u);
  assert.match(details.textContent, /child-full-id/);
  assert.match(root.querySelector('.context-rail-card[aria-current="true"]').textContent, /分析方案/);
});
