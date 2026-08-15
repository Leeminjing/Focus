"use strict";
// app-*.test.cjs 共享的 VM 测试脚手架（change 13）。
// 统一:inert stub、document stub（selector 分支表）、vm context 骨架、app.js 加载。
// 差异点(statusNode 记录、selector 分支、fetch 收集、额外全局)经 createAppHarness(options) 传入。
const fs = require("node:fs");
const vm = require("node:vm");

// 惰性 stub:任何属性/方法访问都安全
const inert = {
  addEventListener() {},
  classList: { toggle() {} },
  content: { cloneNode() {} },
};

const BASE_GLOBALS = {
  Headers,
  clearInterval() {},
  clearTimeout() {},
  console,
  location: { origin: "http://localhost", protocol: "http:" },
  requestAnimationFrame() {},
  setTimeout() {},
  window: {},
};

function readAppSource() {
  return fs
    .readFileSync(require.resolve("./app.js"), "utf8")
    .replace(/bootstrap\(\);\s*$/, "");
}

function createAppHarness(options = {}) {
  // options:
  //   selectors: 额外 document.querySelector 分支(selector → 值或 () => 值),覆盖默认 #app/#globalStatus
  //   statusNode: "record" 时记录 textContent / classList.toggle("danger"),状态暴露在 harness.statusState
  //   fetch: true 时注入 fetch/EventSource/FormData,调用收集到 harness.fetches(可被测试 setGlobal 替换)
  //   globals: 额外 vm 全局(数组引用等)
  const statusState = { text: "", danger: false };
  const statusNode =
    options.statusNode === "record"
      ? {
          set textContent(value) { statusState.text = value; },
          get textContent() { return statusState.text; },
          classList: { toggle(cls, on) { if (cls === "danger") statusState.danger = on; } },
        }
      : { textContent: "", classList: { toggle() {} } };
  const selectors = { "#app": { dataset: {} }, "#globalStatus": statusNode, ...(options.selectors || {}) };
  const document = {
    body: { dataset: {} },
    addEventListener() {},
    querySelector(selector) {
      const value = selectors[selector];
      return typeof value === "function" ? value() : value || inert;
    },
    querySelectorAll() { return []; },
  };
  const globals = { ...BASE_GLOBALS, document, ...(options.globals || {}) };
  const fetches = [];
  if (options.fetch) {
    globals.fetch = async (url, opts = {}) => {
      fetches.push({ url, options: opts });
      return { ok: true, status: 200, json: async () => ({ run_id: "run-1", status: "pending" }) };
    };
    globals.EventSource = class { addEventListener() {} };
    globals.FormData = class {};
  }
  const context = vm.createContext(globals);
  // window === globalThis(浏览器语义):app.js 经 window.markdownit 访问渲染器
  context.window = context;
  // markdown-it(vendor):与浏览器一致,script 内容在 context 内执行,暴露全局 markdownit
  vm.runInContext(fs.readFileSync(require.resolve("./vendor/markdown-it.min.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(require.resolve("./context-editor.js"), "utf8"), context);
  return { vm, context, document, inert, statusNode, statusState, fetches };
}

module.exports = { createAppHarness, readAppSource, inert };
