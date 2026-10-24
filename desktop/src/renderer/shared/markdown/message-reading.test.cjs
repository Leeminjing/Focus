/* Regression at the production Context entry point, with task/robot reader parity. */
const assert = require("node:assert/strict");
const test = require("node:test");
const reading = require("./message-reading.js");
const contextView = require("../../features/context/context-conversation-view.js");
const { createAppHarness, readAppSource } = require("../../../../tests/helpers/test-helper.cjs");
const source = '## 结果\n\n- 列表\n\n| 文件 | 状态 |\n| --- | --- |\n| a.js | 完成 |\n\n[文件](file:///C:/work/a.js)\n\n```js\n\t  x < 2 && y\n```\n';
const message = { role: "assistant", content: [{ type: "text", text: source }], tool_calls: [{ name: "read_file", args: { path: "a.js" } }] };

test("Context inspection renders the same safe reading HTML as task and robot answers", () => {
  const { vm, context } = createAppHarness();
  vm.runInContext(readAppSource(), context);
  context.source = source;
  const taskBody = vm.runInContext("renderAssistantContent(source)", context);
  const inspection = contextView.renderInspection({ context_id: "ctx", revision: { revision_id: "r-7" }, total: 1, messages: [{ index: 0, message }] });
  const robot = reading.render({ role: "assistant", content: source });
  assert.ok(inspection.includes(taskBody));
  assert.ok(robot.includes(taskBody));
  for (const html of [inspection, robot, taskBody]) {
    for (const pattern of [/<h2>结果<\/h2>/, /<ul>/, /<table>/, /href="file:\/\/\/C:\/work\/a.js"/, /<pre><code class="language-js">\t  x &lt; 2 &amp;&amp; y\n/]) assert.match(html, pattern);
  }
  assert.match(inspection, /<details class="message-tool"><summary><strong>工具输入 · read_file/);
  assert.match(inspection, /<details class="message-audit">/);
  assert.match(inspection, /r-7/);
  assert.doesNotMatch(inspection, /<details[^>]*\bopen\b|loopInterventionForm/);
});

test("tool results and complete raw payload/provenance stay available behind closed details", () => {
  const html = contextView.renderInspection({ revision: { revision_id: "fixed-revision" }, messages: [{ index: 3, provenance: { source_kind: "delegated_patrol", source_id: "origin-1" }, message: { role: "tool", name: "shell", content: "long output\n".repeat(100), tool_call_id: "call-1" } }] });
  assert.match(html, /<details class="message-tool"><summary><strong>工具输出 · shell/);
  assert.match(html, /call-1/);
  assert.match(html, /origin-1/);
  assert.match(html, /fixed-revision/);
  assert.doesNotMatch(html, /<details[^>]*\bopen\b/);
  assert.match(html, /long output\nlong output/);
});

test("read-only inspection retains compression audit without restore controls", () => {
  const html = contextView.renderInspection({ messages: [{ index: 0, message: { id: "compressed", role: "ai", content: "摘要", compression: { source: [{ role: "ai", content: "原文" }] } } }] });
  assert.match(html, /原文/);
  assert.doesNotMatch(html, /loop-restore-compression/);
});

test("cached Markdown respects equal-length edits, translated controls and rejects injected HTML/links", () => {
  let language = "zh";
  const reader = reading.create({ translate: (_key, fallback) => language === "zh" ? fallback : "Copy code" });
  assert.match(reader.renderContent("## aa"), /<h2>aa<\/h2>/);
  assert.match(reader.renderContent("## bb"), /<h2>bb<\/h2>/);
  assert.equal(reader.renderContent(source), reader.renderContent(source));
  language = "en";
  assert.match(reader.renderContent(source), />Copy code<\/button>/);
  const html = reader.renderContent('<script>alert(1)</script>\n\n[x](javascript:alert(1))\n\n[x](data:text/html,bad)');
  assert.doesNotMatch(html, /<script|href="(?:javascript|data):/);
  assert.match(html, /&lt;script&gt;/);
});
