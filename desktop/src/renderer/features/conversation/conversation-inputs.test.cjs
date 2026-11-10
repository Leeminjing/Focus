/** 耐久消息状态渲染：转义正文，只允许未派发队列撤回。 */
const test = require("node:test"), assert = require("node:assert/strict"), vm = require("node:vm"), fs = require("node:fs"), path = require("node:path");
const context = vm.createContext({ window: {} });
vm.runInContext(fs.readFileSync(path.join(__dirname, "./conversation-inputs.js"), "utf8"), context);
const view = context.window.FocusConversationInputs;
const escape = value => String(value).replaceAll("<", "&lt;").replaceAll('"', "&quot;");
test("queued and blocked messages remain editable; delivered or started messages do not", () => {
  const rows = ["accepted", "queued", "blocked", "delivered", "started", "not_delivered"].map(status => ({
    input_id: status, mode: ["queued", "blocked", "started"].includes(status) ? "queue" : "steer", status,
    run: { message: "<script>" }, error: status === "blocked" ? "<error>" : null,
  }));
  const html = view.render(rows, escape);
  assert.equal((html.match(/data-action="edit-queued-main"/g) || []).length, 2);
  assert.equal((html.match(/data-action="withdraw-queued-main"/g) || []).length, 2);
  assert.ok(html.includes("已进入当前轮上下文"));
  assert.ok(html.includes("本轮结束前未送达"));
  assert.ok(!html.includes("<script>") && !html.includes("<error>"));
});

test("pending and unsuccessful inputs stay visible; terminal inputs form collapsed history without mutating receipts", () => {
  const rows = ["delivered", "queued", "started", "accepted", "blocked", "not_delivered", "withdrawn", "failed"].map(status =>
    Object.freeze({ input_id: status, status, mode: "queue", run: Object.freeze({ message: status }) }));
  Object.freeze(rows);
  const before = JSON.stringify(rows);
  const html = view.render(rows, escape);
  const [pending, history] = html.split('<details class="conversation-input-history"');
  for (const status of ["accepted", "queued", "blocked", "not_delivered", "failed"]) {
    assert.match(pending, new RegExp(`data-input-status="${status}"`));
    assert.doesNotMatch(history, new RegExp(`data-input-status="${status}"`));
  }
  for (const status of ["delivered", "started", "withdrawn"]) {
    assert.doesNotMatch(pending, new RegExp(`data-input-status="${status}"`));
    assert.match(history, new RegExp(`data-input-status="${status}"`));
  }
  assert.match(pending, /待处理输入 · 5/);
  assert.match(history, /^><summary>投递历史 · 3/);
  assert.doesNotMatch(history, /data-action/);
  assert.match(pending, />撤回<\/button>/);
  assert.doesNotMatch(html, />删除</);
  assert.equal(JSON.stringify(rows), before);
});

test("empty and history-only panels have no pending section; expanded history can survive refresh", () => {
  assert.equal(view.render([], escape), "");
  const rows = [{ input_id: "done", mode: "queue", status: "started", run: { message: "done" } }];
  assert.doesNotMatch(view.render(rows, escape), /conversation-input-pending|data-action/);
  assert.match(view.render(rows, escape, true), /<details class="conversation-input-history" open>/);
});

test("steer receipts never gain withdrawal controls, even with a queue status", () => {
  const html = view.render(["queued", "blocked", "accepted", "not_delivered"].map(status => ({
    input_id: status, mode: "steer", status, run: { message: "supplement" },
  })), escape);
  assert.doesNotMatch(html, /data-action/);
});
