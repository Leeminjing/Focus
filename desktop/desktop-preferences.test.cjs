"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");
const os = require("node:os");
const path = require("node:path");
const { DesktopPreferences, registerDesktopPreferences } = require("./desktop-preferences.cjs");

function repository(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "focus-preferences-"));
  t.after(() => {
    assert.ok(path.resolve(directory).startsWith(path.resolve(os.tmpdir()) + path.sep));
    fs.rmSync(directory, { recursive: true, force: true });
  });
  return { directory, store: new DesktopPreferences(directory) };
}

function preload(directory, localStorage, authorize = () => {}) {
  const handlers = new Map();
  registerDesktopPreferences({ on: (name, fn) => handlers.set(name, fn) }, { directory: () => directory, authorize });
  let bridge;
  vm.runInNewContext(fs.readFileSync(require.resolve("./preload.cjs"), "utf8"), {
    process: { argv: [], env: {} }, window: { localStorage, addEventListener() {} },
    require: () => ({ contextBridge: { exposeInMainWorld: (_name, api) => { bridge = api; } },
      ipcRenderer: { sendSync(channel, ...args) { const event = {}; handlers.get(channel)(event, ...args); return event.returnValue; } } }),
  });
  return bridge.preferences;
}

test("真实 preload 将当前来源旧偏好迁入磁盘，换来源后固定仓库优先", t => {
  const { directory, store } = repository(t);
  const legacy = new Map([["focus-interface-language-v1", "en-US"]]);
  const storage = { getItem: key => legacy.get(key) ?? null, removeItem: key => legacy.delete(key) };
  const first = preload(directory, storage);
  assert.equal(first.getItem("focus-interface-language-v1"), "en-US");
  assert.equal(legacy.size, 0);
  assert.equal(store.getItem("focus-interface-language-v1"), "en-US");
  const next = preload(directory, { getItem: () => "zh-CN" });
  assert.equal(next.getItem("focus-interface-language-v1"), "en-US");
  next.setItem("focus-zoom-level", "3");
  assert.equal(new DesktopPreferences(directory).getItem("focus-zoom-level"), "3");
});

test("授权与键白名单限制真实桥；磁盘失败不删除旧来源或覆盖已保存偏好", t => {
  const { directory, store } = repository(t);
  let allowed = true;
  const values = new Map([["focus-panel-width", "480"]]);
  const bridge = preload(directory, { getItem: key => values.get(key) ?? null, removeItem: key => values.delete(key) },
    () => { if (!allowed) throw new Error("unauthorized"); });
  bridge.setItem("focus-zoom-level", "2");
  assert.throws(() => bridge.setItem("../../outside", "oops"), /偏好键无效/);
  assert.throws(() => bridge.setItem("focus-panel-width", "x".repeat(8193)), /过长/);
  allowed = false;
  assert.throws(() => bridge.getItem("focus-zoom-level"), /unauthorized/);
  assert.throws(() => bridge.setItem("focus-zoom-level", "3"), /unauthorized/);
  allowed = true;
  const rename = t.mock.method(fs, "renameSync", () => { throw new Error("disk full"); });
  assert.throws(() => bridge.getItem("focus-panel-width"), /disk full/);
  assert.equal(values.get("focus-panel-width"), "480");
  assert.throws(() => bridge.setItem("focus-zoom-level", "3"), /disk full/);
  rename.mock.restore();
  assert.equal(store.getItem("focus-zoom-level"), "2");
});

test("工作台和语言从桌面仓库读取跨端口偏好", () => {
  const values = new Map([
    ["focus-interface-language-v1", "en-US"],
    ["focus-zoom-level", "2"],
    ["focus-shell-layout", JSON.stringify({ navWidth: 240, inspectorWidth: 380, navCollapsed: true })],
    ["focus-panel-width", "480"],
  ]);
  const preferences = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
  const h = createAppHarness({ globals: { focusDesktop: { preferences } } });
  vm.runInContext(fs.readFileSync(require.resolve("./i18n.js"), "utf8"), h.context);
  vm.runInContext(readAppSource(), h.context);
  const read = code => vm.runInContext(code, h.context);
  assert.equal(read("interfaceI18n.locale()"), "en-US");
  assert.equal(read("state.zoomLevel"), 2);
  assert.equal(read("state.panelWidth"), 480);
  assert.equal(read("state.shellLayout.navWidth"), 240);
  assert.equal(read("state.shellLayout.navCollapsed"), true);
  read("interfaceI18n.setLocale('zh-CN'); applyZoomLevel(3); state.shellLayout.navWidth=260; persistShellLayout()");
  assert.equal(values.get("focus-interface-language-v1"), "zh-CN");
  assert.equal(values.get("focus-zoom-level"), "3");
  assert.equal(JSON.parse(values.get("focus-shell-layout")).navWidth, 260);
});
