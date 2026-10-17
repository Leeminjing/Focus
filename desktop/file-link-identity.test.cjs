"use strict";
const assert = require("node:assert/strict");
const { createAppHarness, createNode, readAppSource } = require("./test-helper.cjs");

const harness = createAppHarness({ statusNode: "record" });
harness.vm.runInContext(readAppSource(), harness.context);
harness.vm.runInContext(`
  state.activeTaskId = 'task-a';
  state.tasks = [{ task_id: 'task-a', workspace_id: 'workspace-a' }];
  state.materials.set('task-a', [
    { material_id: 'root', relative_path: 'report.md' },
    { material_id: 'docs', relative_path: 'docs/report.md' },
  ]);
  pluginViewForMaterial = () => ({});
  render = () => {};
`, harness.context);
const anchor = {
  getAttribute: () => "file:///C:/workspace/docs/report.md",
  closest: () => null,
};
harness.dispatch("click", {
  target: { closest: selector => selector === "a[href]" ? anchor : null },
  preventDefault() {}, stopPropagation() {},
});
const target = harness.vm.runInContext("state.filesPanel", harness.context);
assert.equal(target?.path, "C:/workspace/docs/report.md", "点击 file URL 必须保留完整路径，不能打开根目录同名材料");
assert.notEqual(target?.material_id, "root");

function clickLink(href, context = null) {
  const source = context ? { dataset: { fileContext: JSON.stringify(context), fileTaskId: "task-a" } } : null;
  const link = { dataset: {}, getAttribute: () => href, closest: () => source };
  let prevented = false;
  harness.dispatch("click", { target: { closest: selector => selector === "a[href]" ? link : null },
    preventDefault() { prevented = true; }, stopPropagation() {} });
  return { prevented, target: harness.vm.runInContext("state.filesPanel", harness.context) };
}

for (const [href, path] of [
  ["docs/report.md", "docs/report.md"],
  ["其他/report.md", "其他/report.md"],
  ["docs/%E4%B8%AD%E6%96%87%20%E6%8A%A5%E5%91%8A.md", "docs/中文 报告.md"],
  ["file:///C:/work%20tree/docs/%E4%B8%AD%E6%96%87.md#L20", "C:/work tree/docs/中文.md"],
  ["D:\\work tree\\docs\\report.md", "D:/work tree/docs/report.md"],
  ["file://localhost/C:/docs/report.md", "C:/docs/report.md"],
  ["file://server/share/docs/report.md", "//server/share/docs/report.md"],
  ["docs/%2520.md", "docs/%20.md"],
]) {
  const result = clickLink(href);
  assert.equal(result.prevented, true);
  assert.equal(result.target.path, path);
  assert.equal(result.target.workspace_id, "workspace-a");
  assert.equal(result.target.view, "current");
}
assert.equal(clickLink("report.md", { slot_id: "tree-one", run_id: "run-one" }).target.slot_id, "tree-one");
assert.equal(clickLink("report.md", { slot_id: "tree-two", run_id: "run-two" }).target.slot_id, "tree-two");
assert.equal(clickLink("report.md", { fact_id: "old", revision: 1, sha256: "hash" }).target.view, "history");
const before = harness.vm.runInContext("state.filesPanel", harness.context);
clickLink("docs/%XY.md");
assert.strictEqual(harness.vm.runInContext("state.filesPanel", harness.context), before);
assert.equal(clickLink("blob:http://localhost/download").prevented, false, "导出的下载链接应保持原行为");
assert.match(harness.statusState.text, /文件链接无效/);
clickLink("report.md", { workspace_id: "foreign" });
assert.strictEqual(harness.vm.runInContext("state.filesPanel", harness.context), before);

const html = harness.context.renderFileCards({ files: [{ filename: "report.md", path: "docs/report.md", slot_id: "tree-one", version_id: "old" }] });
const cardsRoot = harness.document.createElement("div");
cardsRoot.innerHTML = html;
const card = cardsRoot.querySelector('[data-action="open-file-panel"]');
// test-dom stores raw attribute entities; browsers decode these on parsing.
card.dataset.fileReference = card.dataset.fileReference.replaceAll("&quot;", '"');
assert.equal(JSON.parse(card.dataset.fileReference).path, "docs/report.md");
harness.context.openFileCard(card);
assert.equal(harness.vm.runInContext("state.filesPanel.path", harness.context), "docs/report.md");
assert.equal(harness.vm.runInContext("state.filesPanel.version_id", harness.context), "old");

const relativeMarkup = harness.context.renderAssistantContent("[报告](docs/report.md)");
assert.match(relativeMarkup, /href="docs\/report.md"/);
assert.doesNotMatch(harness.context.renderAssistantContent("[bad](javascript:alert(1))"), /href="javascript/);

async function testVersionPreview() {
  const requests = [];
  const h = createAppHarness({ globals: { URL, Response, fetch: async (url, options) => {
    const reference = JSON.parse(options.body);
    requests.push({ url, options, reference });
    if (reference.view === "history") return new Response(JSON.stringify({ detail: "此历史版本未保存对应内容" }), { status: 409 });
    return new Response("latest", { headers: { "Content-Type": "text/plain", "X-Focus-File-Reference": encodeURIComponent(JSON.stringify(reference)) } });
  } } });
  h.vm.runInContext(readAppSource(), h.context);
  h.vm.runInContext(`state.activeTaskId = 't'; state.tasks = [{task_id:'t',workspace_id:'w'}]; render = () => {}; openFileLink({path:'docs/report.md', view:'history', version_id:'v'});`, h.context);
  const handlers = {};
  const container = createNode({ querySelector: selector => ({ addEventListener: (_, callback) => { handlers[selector] = callback; } }) });
  await h.context.mountFileLinkPanel(container, h.vm.runInContext("state.filesPanel", h.context));
  assert.match(container.innerHTML, /历史版本/);
  assert.match(container.innerHTML, /未保存对应内容/);
  assert.doesNotMatch(container.innerHTML, /<pre>latest/);
  assert.equal(requests.length, 1, "历史读取失败不能重试读取最新磁盘文件");
  handlers["[data-file-link-current]"]();
  await h.context.mountFileLinkPanel(container, h.vm.runInContext("state.filesPanel", h.context));
  assert.match(container.innerHTML, /当前磁盘文件/);
  assert.match(container.innerHTML, /<pre>latest/);
  assert.equal(requests[1].reference.version_id, undefined);
  assert.equal(requests[1].reference.path, "docs/report.md");
  assert.equal(requests[1].options.headers["X-Focus-Session"], "focus-dev-session");

  // A late read from another file must not replace the selected file preview.
  let resolveOld;
  h.context.fetch = () => new Promise(resolve => { resolveOld = resolve; });
  h.context.openFileLink({ path: "old.md" });
  const pending = h.context.mountFileLinkPanel(container, h.vm.runInContext("state.filesPanel", h.context));
  h.context.openFileLink({ path: "selected.md" });
  const beforeLate = container.innerHTML;
  resolveOld(new Response("late old content", { headers: { "Content-Type": "text/plain", "X-Focus-File-Reference": "%7B%7D" } }));
  await pending;
  assert.equal(container.innerHTML, beforeLate);

  // Clicking a displayed Fact revision must request that revision, even if a
  // newer revision exists on the server by the time the click is handled.
  let selectedRevision;
  const factHarness = createAppHarness({ globals: { FocusLoopApi: { create: () => ({
    factDetail: async (_loop, _fact, _signal, revision) => { selectedRevision = revision; throw new Error("stop after request"); },
  }) } } });
  factHarness.vm.runInContext(readAppSource(), factHarness.context);
  factHarness.vm.runInContext("state.loop.loopId = 'loop';", factHarness.context);
  const button = { dataset: { action: "inspect-loop-fact", factId: "fact" },
    closest: selector => selector === "[data-fact-revision]" ? { dataset: { factRevision: "3" } } : null };
  await assert.rejects(factHarness.context.handleDocumentClick({ target: {
    closest: selector => selector === "[data-action]" ? button : null,
  } }), /stop after request/);
  assert.equal(selectedRevision, 3);
}
testVersionPreview().then(() => console.log("file-link-identity.test.cjs OK")).catch(error => { console.error(error); process.exitCode = 1; });
