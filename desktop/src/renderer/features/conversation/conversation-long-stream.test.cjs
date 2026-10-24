/* 长回复跨越解析上限时的连续显示、按需展开与快照交接回归。 */
"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const { createAppHarness, readAppSource } = require("../../../../tests/helpers/test-helper.cjs");

function setup() {
  const frames = [];
  const harness = createAppHarness({ fetch: true, globals: {
    requestAnimationFrame: callback => { frames.push(callback); return frames.length; },
  } });
  harness.vm.runInContext(readAppSource(), harness.context);
  const run = code => harness.vm.runInContext(code, harness.context);
  run(`
    globalThis.__task = { task_id: "long-stream", thread_id: "th", workspace_id: "ws", harness_mode: "workspace" };
    state.tasks = [__task]; state.activeTaskId = __task.task_id; state.view = "focus";
    state.streamBuffers = new Map(); state.streamFrames = new Map();
    globalThis.__conv = document.querySelector("#conversation");
    __conv.replaceChildren();
    // syncBlocks 逐块插入元素，块间换行在两条路径中的序列化不同。
    globalThis.__normalize = html => html.replace(/>\\s+</g, "><").trim();
    globalThis.__click = target => handleDocumentClick({ target, preventDefault() {}, stopPropagation() {} });
    globalThis.__send = (content, messageId = "answer") => appendToken({
      thread_id: "th", workspace_id: "ws", agent_id: "main:long-stream", run_id: "run-long",
      data: { content, message_id: messageId },
    });
    globalThis.__sync = () => conversationView.syncStreamingPlaceholder(
      document.querySelector("#conversation"), "run-long", state.streamBuffers.get("run-long"));
  `);
  return { run, flush: () => { while (frames.length) frames.shift()(); } };
}

test("assistant 正文从 65535 增长到 65538 后，已显示的 Markdown 节点原位保留", () => {
  const { run, flush } = setup();
  run(`__send("**保留块**\\n\\n" + "a".repeat(65535 - "**保留块**\\n\\n".length));`);
  flush();
  run(`globalThis.__article = __conv.querySelector("[data-stream-run]");
    globalThis.__body = __article.querySelector(".message-rich");
    globalThis.__nodes = [...__body.children];
    globalThis.__html = __body.innerHTML;
    globalThis.__parses = 0;
    const parse = mdRenderer.parse;
    mdRenderer.parse = function (...args) { __parses++; return parse.apply(this, args); };
    __send("xyz");`);
  flush();
  assert.equal(run(`state.streamBuffers.get("run-long").text.length`), 65538);
  assert.equal(run(`__article.querySelector(".message-rich") === __body`), true, "正文容器不能消失");
  assert.equal(run(`__nodes.every((node, i) => __body.children[i] === node)`), true, "已显示块须原位保留");
  assert.equal(run(`__body.innerHTML === __html`), true);
  assert.match(run(`__article.textContent`), /内容较长，仍在生成/);
  assert.equal(run(`Boolean(__article.querySelector('[data-action="expand-stream-content"]'))`), true);
  run(`for (let i = 0; i < 20; i++) { __send("继续"); __sync(); }
    globalThis.__rerendered = renderStreamingContent(state.streamBuffers.get("run-long"));`);
  assert.equal(run(`__parses`), 0, "超限追加与整体渲染均不得自动重新解析正文");
  assert.equal(run(`__normalize(__rerendered).includes(__normalize(__html))`), true, "两条渲染路径必须保留相同正文");
});

test("超限后完整累积，点击展开读取最新全文，后续增量继续暂停解析，完成快照恢复全文", () => {
  const { run, flush } = setup();
  run(`__send("# 报告\\n\\n" + ("正文。".repeat(20) + "\\n\\n").repeat(1500));`);
  flush();
  run(`__send("\\n\\n**最后追加** <script>alert(1)</script>");`);
  flush();
  run(`globalThis.__buffer = state.streamBuffers.get("run-long");
    globalThis.__full = __buffer.text;
    globalThis.__button = __conv.querySelector('[data-action="expand-stream-content"]');
    __click(__button);`);
  assert.equal(run(`__normalize(__conv.querySelector("[data-stream-run] .message-rich").innerHTML) === __normalize(mdRenderer.render(__full))`), true);
  assert.doesNotMatch(run(`__conv.querySelector("[data-stream-run] .message-rich").innerHTML`), /<script>/);
  run(`globalThis.__expandedHtml = __conv.querySelector("[data-stream-run] .message-rich").innerHTML;
    __send("\\n\\n更新后的末尾");`);
  flush();
  assert.equal(run(`__buffer.text.endsWith("更新后的末尾")`), true, "超限后不能丢弃后续正文增量");
  assert.equal(run(`__conv.querySelector("[data-stream-run] .message-rich").innerHTML`), run(`__expandedHtml`));
  run(`renderConversation({ messages: [] }, __task);
    __click(__conv.querySelector('[data-action="expand-stream-content"]'));
    globalThis.__expandedConversation = renderConversation({ messages: [] }, __task);`);
  assert.equal(run(`__expandedConversation.includes("更新后的末尾")`), true, "整体渲染缓存必须包含展开后的正文");
  run(`replaceConversation(__task, [{ id: "answer", role: "assistant", content: __buffer.text }], "run-long");`);
  assert.equal(run(`__conv.querySelectorAll("[data-stream-run]").length`), 0);
  assert.equal(run(`state.streamBuffers.size`), 0);
  assert.match(run(`document.querySelector("#conversation").textContent`), /更新后的末尾/);
  assert.equal(run(`Boolean(__conv.querySelector('[data-action="expand-stream-content"]'))`), false);
});

test("首帧即超限只解析有界前缀，消息切换清除旧预览与展开状态", () => {
  const { run, flush } = setup();
  run(`globalThis.__lengths = [];
    const parse = mdRenderer.parse;
    mdRenderer.parse = function (source, ...args) { __lengths.push(source.length); return parse.call(this, source, ...args); };
    __send("首帧内容。".repeat(20000));`);
  flush();
  assert.equal(run(`__lengths.length > 0 && __lengths.every(length => length <= FocusConversationRender.STREAM_TEXT_LIMIT)`), true);
  assert.match(run(`__conv.querySelector("[data-stream-run]").textContent`), /首帧内容/);
  run(`__click(__conv.querySelector('[data-action="expand-stream-content"]'));`);
  assert.equal(run(`state.streamBuffers.get("run-long").streamExpandedVersion`), 1);
  run(`__send("新消息", "answer-next");`);
  flush();
  assert.equal(run(`__conv.querySelector("[data-stream-run] .message-rich").textContent.trim()`), "新消息");
  assert.doesNotMatch(run(`__conv.querySelector("[data-stream-run]").textContent`), /首帧内容|内容较长/);
  assert.equal(run(`state.streamBuffers.get("run-long").streamExpandedVersion`), 0);
});

test("恰好 65536 仍正常解析，长代码块跨限后保留并可展开到完整闭合代码", () => {
  const { run, flush } = setup();
  run(`globalThis.__fence = String.fromCharCode(96).repeat(3);
    globalThis.__head = "前文段落\\n\\n" + __fence + "js\\n";
    __send(__head + "x".repeat(65535 - __head.length));`);
  flush();
  run(`__send("y");`);
  flush();
  assert.equal(run(`state.streamBuffers.get("run-long").text.length`), 65536);
  assert.equal(run(`Boolean(__conv.querySelector(".streaming-overflow"))`), false);
  assert.equal(run(`__conv.querySelector("pre code").textContent.trim().endsWith("y")`), true);
  run(`globalThis.__code = __conv.querySelector("pre");
    __send("z\\n" + __fence + "\\n\\n**完成代码后的正文**");`);
  flush();
  assert.equal(run(`__conv.querySelector("pre") === __code`), true);
  run(`__click(__conv.querySelector('[data-action="expand-stream-content"]'));`);
  assert.equal(run(`__conv.querySelector("pre code").textContent.trim().endsWith("yz")`), true);
  assert.match(run(`__conv.querySelector(".message-rich strong").textContent`), /完成代码后的正文/);
});

test("同一 run 切换到另一条超长消息时不复用旧正文", () => {
  const { run, flush } = setup();
  run(`__send("旧消息".repeat(30000));`);
  flush();
  run(`__click(__conv.querySelector('[data-action="expand-stream-content"]'));
    __send("新消息".repeat(30000), "answer-next");`);
  flush();
  assert.match(run(`__conv.querySelector(".message-rich").textContent`), /新消息/);
  assert.doesNotMatch(run(`__conv.querySelector(".message-rich").textContent`), /旧消息/);
  assert.equal(run(`state.streamBuffers.get("run-long").streamExpandedVersion`), 0);
});
