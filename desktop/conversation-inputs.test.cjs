/** 耐久消息状态渲染：转义正文，只允许未派发队列撤回。 */
const test = require("node:test"), assert = require("node:assert/strict"), vm = require("node:vm"), fs = require("node:fs"), path = require("node:path");
const context = vm.createContext({ window: {} });
vm.runInContext(fs.readFileSync(path.join(__dirname, "conversation-inputs.js"), "utf8"), context);
const view = context.window.FocusConversationInputs;
test("queued and blocked messages remain editable; delivered or started messages do not", () => {
  const escape = value => String(value).replaceAll("<", "&lt;").replaceAll('"', "&quot;");
  const rows = ["accepted", "queued", "blocked", "delivered", "started", "not_delivered"].map(status => ({
    input_id: status, mode: ["queued", "blocked", "started"].includes(status) ? "queue" : "steer", status,
    run: { message: "<script>" }, error: status === "blocked" ? "<error>" : null,
  }));
  const html = view.render(rows, escape);
  assert.equal((html.match(/data-action="edit-queued-main"/g) || []).length, 2);
  assert.equal((html.match(/data-action="delete-queued-main"/g) || []).length, 2);
  assert.ok(html.includes("已进入当前轮上下文"));
  assert.ok(html.includes("本轮结束前未送达"));
  assert.ok(!html.includes("<script>") && !html.includes("<error>"));
});
