/* 文件 Diff 的行为回归：直接调用生产比较器与事实详情渲染器。
 * 验证分散修改、行号、空文件/换行、重复行、折叠及计算/渲染上限。
 * 示例：node --test desktop/src/renderer/features/files/file-diff.test.cjs。
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const { buildLineDiff, renderFile } = require("../loop/loop-facts-view.js");
const { createElement } = require("../../../../tests/helpers/test-dom.cjs");

function detail(before, after) {
  return renderFile({ path: "sample.txt", sha256: "a".repeat(64), slot_id: "main",
    before: { available: true, text: before }, after: { available: true, text: after } });
}

function verifyReconstruction(before, after) {
  const diff = buildLineDiff(before, after);
  assert.equal(diff.limited, false);
  assert.equal(diff.rows.filter(row => row.kind !== "added").map(row => row.text).join(""), before);
  assert.equal(diff.rows.filter(row => row.kind !== "removed").map(row => row.text).join(""), after);
  let oldLine = 0, newLine = 0;
  for (const row of diff.rows) {
    assert.equal(row.oldLine, row.kind === "added" ? null : ++oldLine);
    assert.equal(row.newLine, row.kind === "removed" ? null : ++newLine);
  }
  return diff;
}

test("1000 lines with edits at 100 and 900 produce two hunks and exactly +2/-2", () => {
  const left = Array.from({ length: 1000 }, (_, i) => `line ${i + 1}\n`);
  const right = [...left]; right[99] = "changed 100\n"; right[899] = "changed 900\n";
  const before = left.join(""), after = right.join("");
  const diff = verifyReconstruction(before, after);
  assert.equal(diff.added, 2); assert.equal(diff.removed, 2);
  assert.equal(diff.hunks.length, 2);
  assert.deepEqual(diff.hunks.map(({ oldStart, oldCount, newStart, newCount }) => [oldStart, oldCount, newStart, newCount]),
    [[97, 7, 97, 7], [897, 7, 897, 7]]);
  const root = createElement("div"); root.innerHTML = detail(before, after);
  assert.equal(root.querySelectorAll(".file-diff-hunk").length, 2);
  assert.equal(root.querySelectorAll(".removed").length, 2);
  assert.equal(root.querySelectorAll(".added").length, 2);
  assert.match(root.querySelector(".file-diff-summary").textContent, /新增 2 行 · 删除 2 行/);
  const folds = root.querySelectorAll(".file-diff-fold");
  assert.equal(folds.length, 3);
  assert.ok(folds.every(fold => !fold.hasAttribute("open")));
  assert.match(folds[1].querySelector("summary").textContent, /793 行未修改内容/);
  assert.equal(root.querySelector('.is-removed[data-old-line="100"]').getAttribute("data-new-line"), "");
  assert.equal(root.querySelector('.is-added[data-new-line="900"]').getAttribute("data-old-line"), "");
  assert.doesNotMatch(detail(before, after), /未渲染/);
});

test("insertions and deletions retain distinct old/new numbers in later hunks", () => {
  const left = Array.from({ length: 40 }, (_, i) => `line ${i + 1}\n`);
  const right = [...left]; right.splice(29, 1); right.splice(4, 0, "insert a\n", "insert b\n");
  const diff = verifyReconstruction(left.join(""), right.join(""));
  assert.deepEqual([diff.added, diff.removed, diff.hunks.length], [2, 1, 2]);
  assert.deepEqual(diff.rows.filter(row => row.kind === "added").map(row => row.newLine), [5, 6]);
  assert.equal(diff.rows.find(row => row.kind === "removed").oldLine, 30);
  assert.deepEqual([diff.hunks[1].oldStart, diff.hunks[1].newStart], [27, 29]);
});

test("nearby changes merge overlapping context without duplicate rows", () => {
  const before = Array.from({ length: 20 }, (_, i) => `${i}\n`);
  const after = [...before]; after[4] = "four\n"; after[8] = "eight\n";
  const diff = verifyReconstruction(before.join(""), after.join(""));
  assert.deepEqual([diff.hunks.length, diff.added, diff.removed], [1, 2, 2]);
  const root = createElement("div"); root.innerHTML = detail(before.join(""), after.join(""));
  assert.equal(root.querySelectorAll('.file-diff [data-old-line="7"]').length, 1);
});

test("empty files, blank lines and missing final newlines have exact counts", () => {
  for (const [before, after, added, removed] of [
    ["", "", 0, 0], ["", "new\n", 1, 0], ["old\n", "", 0, 1],
    ["", "\n", 1, 0], ["\n\n", "\n", 0, 1], ["a", "a\n", 1, 1],
    ["a\n", "a", 1, 1], ["a\r\nb\r\n", "a\r\nc\r\n", 1, 1],
  ]) {
    const diff = verifyReconstruction(before, after);
    assert.deepEqual([diff.added, diff.removed], [added, removed]);
  }
  assert.match(detail("a", "a\n"), /文件末尾无换行/);
  assert.match(detail("a\r\n", "a\n"), /␍/);
  assert.match(detail("", "new\n"), /@@ -0,0 \+1,1 @@/);
  assert.match(detail("old\n", ""), /@@ -1,1 \+0,0 @@/);
  assert.match(detail("same\n", "same\n"), /无逐行变更 · \+0 \/ −0/);
  assert.doesNotMatch(detail("same\n", "same\n"), /file-diff-hunk/);
});

test("repeated and reordered lines use a shortest edit path", () => {
  // Independent LCS oracle checks minimality; reconstruction checks every token.
  let seed = 17;
  const random = n => { seed = (seed * 1664525 + 1013904223) >>> 0; return Math.floor(seed / 0x100000000 * n); };
  for (let sample = 0; sample < 400; sample++) {
    const left = Array.from({ length: random(18) }, () => `${random(4)}\n`);
    const right = Array.from({ length: random(18) }, () => `${random(4)}\n`);
    const lcs = Array.from({ length: left.length + 1 }, () => new Uint16Array(right.length + 1));
    for (let i = 1; i <= left.length; i++) for (let j = 1; j <= right.length; j++) {
      lcs[i][j] = left[i - 1] === right[j - 1] ? lcs[i - 1][j - 1] + 1 : Math.max(lcs[i - 1][j], lcs[i][j - 1]);
    }
    const diff = verifyReconstruction(left.join(""), right.join(""));
    assert.equal(diff.added + diff.removed, left.length + right.length - 2 * lcs[left.length][right.length]);
  }
});

test("diff escapes both changed and folded lines and keeps unavailable baseline explanation", () => {
  const html = detail("<script>\n" + "<img>\n".repeat(10), "<svg>\n" + "<img>\n".repeat(10));
  assert.doesNotMatch(html, /<(script|img|svg)>/);
  assert.match(html, /class="removed">−&lt;script&gt;/);
  assert.match(html, /class="added">\+&lt;svg&gt;/);
  assert.match(html, /class="equal"> &lt;img&gt;/);
  const missing = renderFile({ path: "file", result_key: "version", before: { available: false, reason: "before_unobserved" },
    after: { available: true, text: "new" } });
  assert.match(missing, /写入前版本未被观察，无法生成可靠 Diff/);
  assert.doesNotMatch(missing, /file-diff-summary|class="file-diff"|新增 \d+ 行/);
});

test("rendering is bounded while file totals include changes beyond displayed rows", () => {
  const before = Array.from({ length: 4000 }, (_, i) => `old ${i}\n`).join("");
  const after = Array.from({ length: 4000 }, (_, i) => `new ${i}\n`).join("");
  const html = detail(before, after);
  assert.match(html, /新增 4000 行 · 删除 4000 行/);
  assert.equal((html.match(/class="file-diff-row /g) || []).length, 1000);
  assert.match(html, /其余 7000 行未渲染/);
  assert.match(detail("a".repeat(10000), "b".repeat(10000)), /此行显示已截断/);
});

test("large sparse files keep exact stats while folding only a bounded amount of context", () => {
  const left = Array.from({ length: 30000 }, (_, i) => `${i}\n`), right = [...left];
  right[100] = "first\n"; right[29000] = "last\n";
  const html = detail(left.join(""), right.join(""));
  assert.match(html, /2 个变更块 · 新增 2 行 · 删除 2 行/);
  assert.match(html, /其余 \d+ 行未渲染/);
  assert.ok((html.match(/class="file-diff-row /g) || []).length < 1100);
  assert.match(html, /class="added">\+last/);
});

test("work, line and input limits report unavailable comparison without fabricated edits or totals", () => {
  const left = Array.from({ length: 1800 }, (_, i) => `${i}\n`);
  const reversed = [...left].reverse();
  assert.equal(buildLineDiff(left.join(""), reversed.join("")).limited, true);
  assert.equal(buildLineDiff("\n".repeat(100001), "").limited, true);
  assert.equal(buildLineDiff("x".repeat(2 * 1024 * 1024 + 1), "").limited, true);
  const html = detail(left.join(""), reversed.join(""));
  assert.match(html, /已停止逐行比较/);
  assert.doesNotMatch(html, /file-diff-summary|class="removed"|class="added"/);
});

test("long collapsed context cannot consume the budget for later changes", () => {
  const left = Array.from({ length: 2000 }, (_, i) => `${i} ${"x".repeat(250)}\n`), right = [...left];
  right[50] = "first\n"; right[1950] = "last\n";
  const html = detail(left.join(""), right.join(""));
  assert.match(html, /class="added">\+first/);
  assert.match(html, /class="added">\+last/);
  assert.match(html, /其余 \d+ 行未渲染/);
});

test("history tabs leave nested unchanged-line folds inside the Diff panel", async () => {
  const fs = require("node:fs"), vm = require("node:vm");
  const { createDocument } = require("../../../../tests/helpers/test-dom.cjs");
  const host = createElement("div");
  host.innerHTML = fs.readFileSync(require.resolve("../../desktop-patrol.html"), "utf8");
  const context = vm.createContext({ AbortController, FocusLoopFactsView: require("../loop/loop-facts-view.js"), document: createDocument() });
  vm.runInContext(fs.readFileSync(require.resolve("./file-links.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(require.resolve("../patrol/companion/desktop-patrol-references.js"), "utf8"), context);
  const left = Array.from({ length: 1000 }, (_, i) => `line ${i + 1}\n`), right = [...left];
  right[99] = "first\n"; right[899] = "last\n";
  const file = { path: "src/sample.txt", result_key: "result", sha256: "a".repeat(64), fact_id: "fact", revision: 1,
    before: { available: true, text: left.join("") }, after: { available: true, text: right.join("") } };
  const refs = context.FocusDesktopPatrolReferences.create({ host, api: { factDetail: async () => ({ file }) },
    loopId: () => "loop", store: () => ({}), onError: error => { throw error; } });
  await refs.detail(file);
  const section = host.querySelector("#version-body .file-version"), tabs = section.querySelector(".version-tabs");
  assert.equal(tabs.children.length, 3);
  const panels = section.querySelectorAll(':scope > [role="tabpanel"]');
  assert.equal(panels.length, 3);
  assert.equal(section.querySelectorAll('[role="tabpanel"]').length, 3);
  assert.equal(panels[1].querySelectorAll("details.file-diff-fold").length, 3);
  assert.ok(panels[1].querySelectorAll("details.file-diff-fold").every(fold => fold.querySelector("summary")));
  assert.match(tabs.children[1].textContent, /Diff · \+2 \/ −2/);
  tabs.children[1].dispatchEvent({ type: "click" });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(panels.map(panel => panel.hidden), [true, false, true]);
  tabs.children[1].dispatchEvent({ type: "keydown", key: "End", preventDefault() {} });
  assert.deepEqual(panels.map(panel => panel.hidden), [true, true, false]);
  tabs.children[2].dispatchEvent({ type: "keydown", key: "Home", preventDefault() {} });
  assert.deepEqual(panels.map(panel => panel.hidden), [false, true, true]);
  refs.dispose();
});
