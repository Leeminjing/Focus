// sendMain 载荷测试：主对话不携带权限勾选，权限由后端 MainRunCreate 默认全开（fix-shell-tool-unavailable）
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

let conversation = { clientHeight: 100, scrollHeight: 1000, scrollTop: 0 };
const app = { dataset: {} };
const fetches = [];

const mainInput = { value: "用 shell 运行命令", disabled: false };

const inert = {
  addEventListener() {},
  classList: { toggle() {} },
  content: { cloneNode() {} },
  hidden: true,
  querySelector() { return null; },
  querySelectorAll() { return []; },
};

const document = {
  body: { dataset: {} },
  addEventListener() {},
  querySelector(selector) {
    if (selector === "#app") return app;
    if (selector === "#conversation") return conversation;
    if (selector === "#mainInput") return mainInput;
    if (selector === "#globalStatus") return { textContent: "", dataset: {}, classList: { toggle() {} } };
    return inert;
  },
  querySelectorAll() { return []; },
};

const fetchStub = async (url, options) => {
  fetches.push({ url, options });
  return { ok: true, json: async () => ({ run_id: "run-1", status: "pending" }) };
};

const context = vm.createContext({
  Headers,
  FormData: class {},
  fetch: fetchStub,
  EventSource: class { addEventListener() {} },
  clearTimeout() {},
  console,
  document,
  location: { origin: "http://localhost", protocol: "http:" },
  setTimeout() {},
  window: {},
});
const source = fs.readFileSync(require.resolve("./app.js"), "utf8").replace(/bootstrap\(\);\s*$/, "");
new vm.Script(source).runInContext(context);

new vm.Script(`
  state.tasks = [{ task_id: "task", workspace_path: "C:/workspace", workspace_name: "workspace", thread_id: "th", workspace_id: "ws" }];
  state.activeTaskId = "task";
  state.equipment.permissions = ["read", "write", "host_command"];
  state.details.set("task", { messages: [], ui_state: {} });
`).runInContext(context);

(async () => {
  await new vm.Script("sendMain()").runInContext(context);
  const runRequest = fetches.find(f => /main\/runs$/.test(f.url));
  assert.ok(runRequest, "sendMain 必须发起 main/runs 请求");
  const body = JSON.parse(runRequest.options.body);
  assert.equal(body.message, "用 shell 运行命令");
  assert.equal("permissions" in body, false, "前端不携带 permissions（后端默认全开 host_command）");
  assert.deepEqual(body.skills, []);
  console.log("✅ sendMain 载荷正确（不携带 permissions，后端默认 read+write+host_command）");
})().catch(err => { console.error("测试失败:", err.stack || err.message); process.exit(1); });
