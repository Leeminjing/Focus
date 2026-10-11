/* 本文件验证运行卡纯预览的身份、消息和有限尾部行为。
 * 输入为实际 Run 信封形状的 tokens/events/metadata/reasoning；输出为角色切换、完整快照去重、恢复基线、断线缺口与内存上限断言。
 * 工作流覆盖 Assistant→Tool→Assistant、多工具、具名 actor 重试，以及初始旧消息不能冒用；示例：node --test desktop/running-context-preview.test.cjs。
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const Preview = require("./running-context-preview.js");
const identity = { run_id: "r", context_id: "c", workspace_id: "w", thread_id: "t", message_id: "input" };
const create = options => Preview.create({ ...identity, ...options });
const frame = (type, id, data, owner = {}) => ({ type, id: String(id), data: JSON.stringify({ run_id: "r", workspace_id: "w", thread_id: "t", data, ...owner }) });
const message = (id, content, rest = {}) => ({ id, role: "ai", content, ...rest });
const values = (id, rows) => frame("events", id, { messages: [message("input", "question", { role: "human" }), ...rows] });
const tokens = (id, content, messageId = "a") => frame("tokens", id, { message_id: messageId, content });
const actor = (id, name, rows, rest = {}) => frame("events", id, { type: "commitment_messages", actor: name, content_mode: "snapshot", messages: rows, ...rest });

test("assistant, waiting tool, result and following assistant remain separate real outputs", () => {
  let state = Preview.reduce(create(), values(1, []));
  state = Preview.reduce(state, tokens(2, "先读取文件。"));
  assert.equal(state.role, "Assistant");
  state = Preview.reduce(state, values(3, [message("a", "先读取文件。", { tool_calls: [{ id: "call", name: "read_file", args: { path: "secret argument" } }] })]));
  assert.equal(state.role, "Tool");
  assert.equal(state.tool_name, "read_file");
  assert.equal(state.status, "tool_wait");
  assert.equal(state.text, "");
  state = Preview.reduce(state, values(4, [message("a", "先读取文件。", { tool_calls: [{ id: "call", name: "read_file" }] }), message("tool-result", "真实结果", { role: "tool", tool_call_id: "call", name: "read_file" })]));
  assert.equal(state.text, "真实结果");
  state = Preview.reduce(state, tokens(5, "接着修改。", "b"));
  assert.equal(state.role, "Assistant");
  assert.equal(state.text, "接着修改。");
});

test("duplicate SSE and snapshots do not duplicate text or revert a named role", () => {
  let state = Preview.reduce(create(), tokens(1, "hello "));
  state = Preview.reduce(state, tokens(2, "world"));
  assert.equal(Preview.reduce(state, tokens(2, "world")), state);
  state = Preview.reduce(state, values(3, [message("a", "hello world")]));
  assert.equal(state.text, "hello world");
  state = Preview.reduce(state, actor(4, "supervisor", [message("s", "请实现")]));
  state = Preview.reduce(state, actor(5, "worker", [message("x", "正在实现")], { content_mode: "delta", stream_id: "worker:1:1" }));
  state = Preview.reduce(state, actor(6, "supervisor", [message("s", "请实现")]));
  assert.equal(state.role, "Worker");
  assert.equal(state.text, "正在实现");
});

test("an earlier streamed message snapshot cannot overwrite a newer message or actor", () => {
  let state = Preview.reduce(create(), tokens(1, "earlier partial", "a"));
  state = Preview.reduce(state, tokens(2, "latest message", "b"));
  state = Preview.reduce(state, values(3, [message("a", "earlier partial completed")]));
  assert.equal(state.text, "latest message");
  state = Preview.reduce(state, actor(4, "worker", [message("worker", "正在工作")], { content_mode: "delta", stream_id: "worker1" }));
  state = Preview.reduce(state, values(5, [message("a", "earlier partial completed"), message("b", "latest message completed")]));
  assert.equal(state.role, "Worker");
  assert.equal(state.text, "正在工作");
});

test("every initial values snapshot is a baseline, including matching older anchors", () => {
  for (const options of [{}, { message_id: "missing" }]) {
    let state = Preview.reduce(create(options), values(1, [message("old", "上一轮的话")]));
    assert.equal(state.text, "");
    assert.equal(state.status, "waiting");
    state = Preview.reduce(state, values(2, [message("old", "上一轮的话"), message("new", "本轮新内容")]));
    assert.equal(state.text, "本轮新内容");
  }
});

test("snapshot-only model shows new output after baseline and subsequent new tool calls", () => {
  let state = Preview.reduce(create(), values(1, []));
  state = Preview.reduce(state, values(2, [message("answer", "完整模型输出")]));
  assert.equal(state.role, "Assistant");
  assert.equal(state.text, "完整模型输出");
  state = Preview.reduce(state, values(3, [message("answer", "完整模型输出", { tool_calls: [{ id: "call", name: "read_file" }] })]));
  assert.equal(state.role, "Tool");
  assert.equal(state.status, "tool_wait");
  assert.equal(state.text, "");
});

test("initial values never erase already streamed text or impersonate old tool activity", () => {
  let state = Preview.reduce(create(), tokens(1, "本轮刚刚输出"));
  const old = message("old", "上一轮输出", { tool_calls: [{ id: "old-call", name: "old_tool" }] });
  state = Preview.reduce(state, values(2, [old]));
  assert.equal(state.role, "Assistant");
  assert.equal(state.text, "本轮刚刚输出");
  state = Preview.reduce(state, values(3, [old, message("new", "", { tool_calls: [{ id: "new-call", name: "read_file" }] })]));
  assert.equal(state.role, "Tool");
  assert.equal(state.tool_name, "read_file");
  assert.equal(state.text, "");
});

test("retained token tail proves the current snapshot and exact tool call, even in the first values", () => {
  for (const withResult of [false, true]) {
    let state = Preview.reduce(create(), tokens(44, "文件。", "current"));
    const current = message("current", "读取文件。", { tool_calls: [{ id: "current-call", name: "read_file" }] });
    const oldResult = message("old-result", "历史工具结果", { role: "tool", tool_call_id: "old-call", name: "old_tool" });
    const result = message("current-result", "真实读取结果", { role: "tool", tool_call_id: "current-call", name: "read_file" });
    state = Preview.reduce(state, values(45, [oldResult, current, ...(withResult ? [result] : [])]));
    assert.equal(state.role, "Tool");
    assert.equal(state.tool_name, "read_file");
    assert.equal(state.status, withResult ? "streaming" : "tool_wait");
    assert.equal(state.text, withResult ? "真实读取结果" : "");
  }
  let state = Preview.reduce(create(), tokens(44, "尾部", "current"));
  state = Preview.reduce(state, values(45, [message("current", "完整消息尾部")]));
  assert.equal(state.text, "完整消息尾部");
});

test("older token evidence does not let first or later values reverse a newer named actor", () => {
  let state = Preview.reduce(create(), tokens(44, "Earlier", "old"));
  state = Preview.reduce(state, actor(45, "worker", [message("work", "当前正在执行")], { content_mode: "delta", stream_id: "worker-now" }));
  const old = message("old", "Earlier full", { tool_calls: [{ id: "old-call", name: "old_tool" }] });
  const result = message("old-result", "旧工具内容", { role: "tool", tool_call_id: "old-call", name: "old_tool" });
  state = Preview.reduce(state, values(46, [old, result]));
  assert.equal(state.role, "Worker");
  assert.equal(state.text, "当前正在执行");
  state = Preview.reduce(state, values(47, [message("old", "Earlier full updated", { tool_calls: [{ id: "late-old-call", name: "older_tool" }] }), result]));
  assert.equal(state.role, "Worker");
  assert.equal(state.text, "当前正在执行");
});

test("named deltas append within stream and retries/snapshot-only evaluator replace", () => {
  let state = Preview.reduce(create(), actor(1, "worker", [message("w", "第一次")], { content_mode: "delta", stream_id: "s1" }));
  state = Preview.reduce(state, actor(2, "worker", [message("w", "输出")], { content_mode: "delta", stream_id: "s1" }));
  assert.equal(state.text, "第一次输出");
  state = Preview.reduce(state, actor(3, "worker", [message("w", "重试")], { content_mode: "delta", stream_id: "s2" }));
  assert.equal(state.text, "重试");
  state = Preview.reduce(state, actor(4, "evaluator", [{ role: "ai", content: "通过" }]));
  state = Preview.reduce(state, actor(5, "evaluator", [{ role: "ai", content: "补充检查" }]));
  assert.equal(state.role, "Evaluator");
  assert.equal(state.text, "补充检查");
});

test("parallel tools retain names and do not re-open completed calls on repeated values", () => {
  const assistant = message("a", "", { tool_calls: [{ id: "x", name: "read_file" }, { id: "y", name: "web_search" }] });
  const y = message("ry", "网页结果", { role: "tool", tool_call_id: "y" });
  const x = message("rx", "文件结果", { role: "tool", tool_call_id: "x" });
  let state = Preview.reduce(create(), values(1, [assistant]));
  state = Preview.reduce(state, values(2, [assistant, y]));
  assert.equal(state.tool_name, "web_search");
  state = Preview.reduce(state, values(3, [assistant, y, x]));
  assert.equal(state.tool_name, "read_file");
  assert.equal(state.text, "文件结果");
  state = Preview.reduce(state, values(4, [assistant, y, x]));
  assert.equal(state.status, "streaming");
});

test("all frame types advance cursor while gaps break concatenation and disconnect stays explicit", () => {
  let state = Preview.reduce(create(), tokens(1, "前半"));
  state = Preview.reduce(state, frame("reasoning", 2, { content: "隐藏思考" }));
  state = Preview.reduce(state, frame("metadata", 3, { status: "running" }));
  state = Preview.reduce(state, tokens(4, "后半"));
  assert.equal(state.text, "前半后半");
  assert.equal(state.notice, "");
  state = Preview.connection(state, "reconnecting");
  assert.equal(state.status, "disconnected");
  assert.equal(Preview.connection(state, "live").status, "disconnected");
  state = Preview.reduce(state, tokens(7, "真实新片段"));
  assert.equal(state.text, "真实新片段");
  assert.match(state.notice, /早前片段不可用/);
  state = Preview.reduce(state, { type: "end", id: "end", data: JSON.stringify({ run_id: "r", status: "success" }) });
  assert.equal(state.status, "ended");
  assert.equal(state.text, "");
  assert.equal(Preview.reduce(state, tokens(8, "迟到输出")), state);
});

test("other owners, system input, reasoning and non-visible blocks cannot become output", () => {
  const start = create();
  assert.equal(Preview.reduce(start, frame("tokens", 1, { content: "wrong", message_id: "m" }, { run_id: "other" })), start);
  assert.equal(Preview.reduce(start, frame("tokens", 1, { content: "wrong", message_id: "m" }, { workspace_id: "other" })), start);
  let state = Preview.reduce(start, values(1, [message("system", "指令", { role: "system" }), message("human", "输入", { role: "human" })]));
  assert.equal(state.text, "");
  state = Preview.reduce(state, values(2, [message("a", [{ type: "text", text: "可见文本" }, { type: "reasoning", text: "不可见" }, { type: "image_url", image_url: "ignored" }])]));
  assert.equal(state.text, "可见文本");
});

test("unicode tail and message fingerprints stay bounded without executing markup", () => {
  let state = Preview.reduce(create(), tokens(1, "😃".repeat(5000) + "<script>literal</script>"));
  assert.equal(Array.from(state.text).length, 4096);
  assert.ok(state.text.endsWith("<script>literal</script>"));
  assert.equal(state.text.charCodeAt(0) >= 0xdc00 && state.text.charCodeAt(0) <= 0xdfff, false);
  for (let id = 2; id <= 300; id++) state = Preview.reduce(state, actor(id, "evaluator", [message(`m${id}`, `result ${id}`)]));
  assert.equal(Object.keys(state.seen).length, 256);
  assert.equal(state.text, "result 300");
});
