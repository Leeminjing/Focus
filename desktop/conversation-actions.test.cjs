/* 验证正文/代码复制的内容边界与失败恢复，以及完整会话 Markdown 导出，不访问系统剪贴板。 */
"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

function setup(writeText = async () => {}) {
  const timers = [];
  const harness = createAppHarness({
    statusNode: "record",
    globals: { navigator: { clipboard: { writeText } }, setTimeout: callback => { timers.push(callback); } },
  });
  harness.vm.runInContext(readAppSource(), harness.context);
  return { ...harness, timers, run: code => harness.vm.runInContext(code, harness.context) };
}

test("assistant copy stores only Markdown body; text blocks are rendered without double escaping", () => {
  const { run } = setup();
  const html = run(`renderMessage({ role: 'assistant', content: [
    { type: 'text', text: '**方案**' },
    { type: 'image_url', image_url: { url: 'private-image' } },
    { type: 'text', text: '\\n\`\`\`js\\n  x < 2 && y\\n\`\`\`' }
  ], tool_calls: [{ name: 'private-tool' }] })`);
  assert.match(html, /data-action="copy-answer"/);
  assert.match(html, /data-copy-text="\*\*方案\*\*/);
  assert.match(html, /x &lt; 2 &amp;&amp; y/);
  assert.doesNotMatch(html, /&amp;lt;|private-image|private-tool/);
  for (const role of ["human", "system", "tool"]) {
    assert.doesNotMatch(run(`renderMessage({ role: '${role}', content: 'text' })`), /copy-answer/);
  }
  assert.doesNotMatch(run(`renderMessage({ role: 'ai', content: '' })`), /copy-answer/);
});

test("fenced, unclosed and indented code have copy controls and escaped language labels", () => {
  const { run, context } = setup();
  for (const source of ["```js\n\t  x < 2 && y\n```", "```\n  unfinished", "    indented\n      code"]) {
    context.source = source;
    assert.match(run("renderAssistantContent(source)"), /data-action="copy-code"/);
    assert.match(run("renderStreamingContent({ text: source })"), /data-action="copy-code"/);
  }
  context.source = '```<img/onerror=alert(1)>\ncode\n```';
  assert.doesNotMatch(run("renderAssistantContent(source)"), /<img/);
  assert.match(run("renderAssistantContent('inline `code`')"), /<code>code<\/code>/);
  assert.doesNotMatch(run("renderAssistantContent('inline `code`')"), /copy-code/);
});

test("delegated copy actions preserve raw body, code indentation and trailing newline, with temporary feedback", async () => {
  const copied = [];
  const { run, context, timers, statusState } = setup(async text => copied.push(text));
  const answer = '**回答**\n\n```sh\n\techo "<ok> & done"\n```\n';
  const code = '\techo "<ok> & done"\n  next\n';
  for (const action of ["copy-answer", "copy-code"]) {
    const button = {
      textContent: action === "copy-answer" ? "复制回答" : "复制代码",
      disabled: false,
      dataset: { action, copyText: answer },
      closest: () => ({ querySelector: () => ({ textContent: code }) }),
    };
    const originalLabel = button.textContent;
    context.event = { target: { closest: selector => selector === "[data-action]" ? button : null } };
    await run("handleDocumentClick(event)");
    assert.equal(copied.at(-1), action === "copy-answer" ? answer : code);
    assert.equal(button.textContent, "已复制");
    assert.equal(statusState.text, "已复制");
    await run("handleDocumentClick(event)");
    assert.equal(copied.length, action === "copy-answer" ? 1 : 2, "feedback期间阻止重复复制");
    timers.pop()();
    assert.equal(button.textContent, originalLabel);
    assert.equal(button.disabled, false);
  }
});

test("clipboard denial or missing API gives failure feedback and allows retry", async () => {
  for (const missing of [false, true]) {
    const { run, context, timers, statusState } = setup(async () => { throw new Error("denied"); });
    if (missing) context.navigator = {};
    const button = { textContent: "复制回答", disabled: false };
    context.button = button;
    await run("copyMessageText(button, 'body')");
    assert.equal(button.textContent, "复制失败，请重试");
    assert.equal(statusState.danger, true);
    timers.pop()();
    assert.equal(button.disabled, false);
    assert.equal(button.textContent, "复制回答");
  }
});

test("export includes earlier and compressed human/assistant bodies in order, omitting tools and UI", () => {
  const { run, context } = setup();
  const messages = Array.from({ length: 100 }, (_, i) => ({ role: i % 2 ? "assistant" : "user", content: `正文 ${i}` }));
  messages.unshift({ role: "ai", content: "compressed-summary", compression: { source: [{ role: "human", content: "原始问题" }, { role: "ai", content: "**原始回答**" }] } });
  messages.push({ role: "system", content: "private-system" }, { role: "tool", content: "private-tool" }, { role: "ai", content: "", tool_calls: [{ name: "private-tool" }] });
  const code = "```python\n\tprint('<ok> & done')\n```";
  messages.push({ role: "ai", content: [{ type: "text", text: code }, { type: "image_url", image_url: { url: "private-image" } }] });
  context.detail = { messages };
  const markdown = run("conversationMarkdown(detail, { title: '测试\\n对话' })");
  assert.ok(markdown.startsWith("# 测试 对话\n"));
  assert.ok(markdown.indexOf("原始问题") < markdown.indexOf("原始回答"));
  assert.ok(markdown.indexOf("正文 0\n") < markdown.indexOf("正文 99\n"));
  assert.ok(markdown.includes(code));
  assert.match(markdown, /## 用户\n\n/);
  assert.match(markdown, /## 助手\n\n/);
  assert.doesNotMatch(markdown, /private-|compressed-summary|复制代码|复制回答/);
});

test("export action downloads UTF-8 Markdown with safe filename and releases its URL", async () => {
  const { run, context, document, timers } = setup();
  let blob;
  let revoked;
  let clicked = false;
  let removed = false;
  const link = { click() { clicked = true; }, remove() { removed = true; } };
  context.Blob = Blob;
  context.URL = { createObjectURL(value) { blob = value; return "blob:conversation"; }, revokeObjectURL(value) { revoked = value; } };
  document.createElement = () => link;
  document.body.append = () => {};
  run(`state.tasks = [{ task_id: 'task', title: '方案:/?. ' }]; state.activeTaskId = 'task'; state.details.set('task', { messages: [{ role: 'ai', content: '**正文**' }] });`);
  context.event = { target: { closest: selector => selector === "[data-action]" ? { dataset: { action: "export-conversation" } } : null } };
  await run("handleDocumentClick(event)");
  assert.equal(link.download, "方案___.md");
  assert.equal(link.href, "blob:conversation");
  assert.equal(blob.type, "text/markdown;charset=utf-8");
  assert.match(await blob.text(), /\*\*正文\*\*/);
  assert.ok(clicked && removed);
  timers.pop()();
  assert.equal(revoked, "blob:conversation");
});
