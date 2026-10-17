/* 流式 Think 的按需详情、增量展开态及 Run/消息隔离回归。 */
"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

function setup() {
  const frames = [];
  const harness = createAppHarness({ fetch: true, globals: {
    requestAnimationFrame: callback => { frames.push(callback); return frames.length; },
  } });
  harness.vm.runInContext(readAppSource(), harness.context);
  const run = code => harness.vm.runInContext(code, harness.context);
  run(`
    globalThis.__task = { task_id: "think", thread_id: "th", workspace_id: "ws", harness_mode: "workspace" };
    state.tasks = [__task]; state.activeTaskId = __task.task_id; state.view = "focus";
    globalThis.__conv = document.querySelector("#conversation");
    __conv.replaceChildren();
    globalThis.__send = (content, messageId = "m1", runId = "r1") => appendReasoning({
      thread_id: "th", workspace_id: "ws", agent_id: "main:think", run_id: runId,
      data: { content, message_id: messageId },
    });
    globalThis.__row = () => __conv.querySelector("[data-stream-run] .is-reasoning");
    globalThis.__open = row => { row.open = true; row.dispatchEvent({ type: "toggle" }); };
  `);
  return { run, flush: () => { while (frames.length) frames.shift()(); } };
}

test("首帧直接展开 Think 得到完整正文，详情保持转义", () => {
  const { run, flush } = setup();
  run(`__send("开头" + "推理内容".repeat(100) + "<script>尾部</script>");`);
  flush();
  run(`__open(__row());`);
  // 最小 DOM 保留 HTML 实体，直接检查转义后的完整详情。
  assert.equal(run(`__row().querySelector(".conversation-event-detail").innerHTML`),
    `<pre>${"开头" + "推理内容".repeat(100)}&lt;script&gt;尾部&lt;/script&gt;</pre>`);
  assert.equal(run(`__row().querySelector("script")`), null);
});

test("后续 token 保留同一 Think 节点和展开态，并刷新完整详情", () => {
  const { run, flush } = setup();
  run(`__send("首段");`);
  flush();
  run(`globalThis.__first = __row(); __open(__first); __send("后续推理");`);
  flush();
  assert.equal(run(`__row() === __first`), true, "增量不得替换推理节点");
  assert.equal(run(`__row().open`), true, "下一条 token 不得收起 Think");
  assert.equal(run(`__row().querySelector(".conversation-event-detail").textContent`), "首段后续推理");
});

test("摘要相同时仍更新已展开详情，收起后更新保持按需加载", () => {
  const { run, flush } = setup();
  run(`__send("x".repeat(150));`);
  flush();
  run(`__open(__row()); globalThis.__summary = __row().querySelector("summary"); __send("x");`);
  flush();
  assert.equal(run(`__row().querySelector("summary") === __summary`), true);
  assert.equal(run(`__row().querySelector("pre").textContent.length`), 151);
  run(`__row().open = false; __row().dispatchEvent({ type: "toggle" }); __send("收起时追加");`);
  flush();
  assert.equal(run(`__row().open`), false);
  assert.equal(run(`__row().querySelector("pre").textContent.length`), 151, "收起期间不渲染全文");
  run(`__open(__row());`);
  assert.equal(run(`__row().querySelector("pre").textContent`), "x".repeat(151) + "收起时追加");
});

test("同一 Run 换消息重置展开态，两条消息详情不串用", () => {
  const { run, flush } = setup();
  run(`__send("旧消息");`);
  flush();
  run(`__open(__row()); globalThis.__old = __row(); __send("新消息", "m2");`);
  flush();
  assert.equal(run(`__row() === __old`), false);
  assert.equal(run(`__row().open`), false);
  assert.equal(run(`__row().querySelector(".conversation-event-detail").innerHTML`), "");
  run(`__open(__row());`);
  assert.equal(run(`__row().querySelector("pre").textContent`), "新消息");
});

test("切换到只有正文的消息时清除旧 Think，后续推理重新按需展开", () => {
  const { run, flush } = setup();
  run(`__send("旧推理");`);
  flush();
  run(`__open(__row()); appendToken({ thread_id: "th", workspace_id: "ws", agent_id: "main:think",
    run_id: "r1", data: { content: "新正文", message_id: "m2" } });`);
  flush();
  assert.equal(run(`__row()`), null);
  assert.equal(run(`__conv.querySelector(".message-rich").textContent.trim()`), "新正文");
  run(`__send("新推理", "m2");`);
  flush();
  assert.equal(run(`__row().open`), false);
  run(`__open(__row());`);
  assert.equal(run(`__row().querySelector("pre").textContent`), "新推理");
});

test("不同 Run 在相同或缺省消息 id 下都有独立详情", () => {
  const { run, flush } = setup();
  run(`__send("Run A", null, "r1"); __send("Run B", null, "r2");`);
  flush();
  run(`globalThis.__rows = [...__conv.querySelectorAll(".is-reasoning")]; __rows.forEach(__open);`);
  assert.notEqual(run(`__rows[0].dataset.eventKey`), run(`__rows[1].dataset.eventKey`));
  assert.equal(run(`__rows[0].querySelector("pre").textContent`), "Run A");
  assert.equal(run(`__rows[1].querySelector("pre").textContent`), "Run B");
  run(`__send("共享 id A", "same", "r1"); __send("共享 id B", "same", "r2");`);
  flush();
  run(`globalThis.__next = [...__conv.querySelectorAll(".is-reasoning")]; __next.forEach(__open);`);
  assert.notEqual(run(`__next[0].dataset.eventKey`), run(`__next[1].dataset.eventKey`));
  assert.equal(run(`__next[0].querySelector("pre").textContent`), "共享 id A");
  assert.equal(run(`__next[1].querySelector("pre").textContent`), "共享 id B");
});

test("整体渲染登记流式详情，随后增量与无关快照保留展开态", () => {
  const { run, flush } = setup();
  run(`state.streamBuffers.set("r1", { taskId: "think", messageId: "m1", reasoning: "整体渲染", text: "" });
    replaceConversation(__task, []);
    __open(__row()); globalThis.__first = __row();`);
  assert.equal(run(`__first.querySelector("pre").textContent`), "整体渲染");
  run(`__send("后续");`);
  flush();
  run(`replaceConversation(__task, [{ id: "human", role: "human", content: "无关快照" }]);`);
  assert.equal(run(`__row() === __first && __row().open`), true);
  assert.equal(run(`__row().querySelector("pre").textContent`), "整体渲染后续");
});

test("超过正文解析上限的推理仍完整累积并能展开最新末尾", () => {
  const { run, flush } = setup();
  run(`__send("x".repeat(FocusConversationRender.STREAM_TEXT_LIMIT));`);
  flush();
  run(`__send("超限后的推理末尾");`);
  flush();
  run(`__open(__row());`);
  assert.equal(run(`__row().querySelector("pre").textContent`), "x".repeat(65536) + "超限后的推理末尾");
});
