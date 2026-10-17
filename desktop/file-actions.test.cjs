"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const { createFileActions } = require("./file-actions.cjs");

test("system open and reveal use the backend-resolved path, never renderer names", async () => {
  const opened = [], revealed = [], requests = [];
  const actions = createFileActions({ shell: { openPath: async p => { opened.push(p); return ""; }, showItemInFolder: p => revealed.push(p) },
    dialog: {}, BrowserWindow: { fromWebContents: () => ({}) }, apiBase: () => "http://local", session: () => "s",
    fetch: async (url, options) => { requests.push([url, JSON.parse(options.body)]); return Response.json({ path: "C:/workspace/report.xlsx" }); } });
  const target = { task_id: "task", content_ref: "report.xlsx" };
  await actions.run({}, { operation: "open", target, name: "C:/untrusted.exe" });
  await actions.run({}, { operation: "reveal", target, name: "C:/untrusted.exe" });
  assert.deepEqual(opened, ["C:/workspace/report.xlsx"]);
  assert.deepEqual(revealed, opened);
  assert.equal(requests[0][1].current, true);
});

test("opening a historical version materializes the exact response with a safe basename", async () => {
  let opened, received;
  const bytes = Buffer.from("saved version\npassword=***");
  const actions = createFileActions({ shell: { openPath: async p => { opened = p; return ""; } }, dialog: {},
    BrowserWindow: { fromWebContents: () => ({}) }, apiBase: () => "http://local", session: () => "s",
    fetch: async (_url, options) => { received = JSON.parse(options.body); return new Response(bytes); } });
  const target = { loop_id: "loop", fact_id: "fact", revision: 2 };
  try {
    await actions.run({}, { operation: "open", target, name: "../../version.txt" });
    assert.deepEqual(received, target);
    assert.deepEqual(await fs.readFile(opened), bytes);
    assert.match(opened, /focus-file-.*[\\/]version\.txt$/);
  } finally { await actions.cleanup(); }
  await assert.rejects(fs.stat(opened), { code: "ENOENT" });
});

test("cancelled save does not fetch or write any file", async () => {
  const actions = createFileActions({ shell: {}, dialog: { showSaveDialog: async () => ({ canceled: true }) },
    BrowserWindow: { fromWebContents: () => ({}) }, apiBase: () => "http://local", session: () => "s",
    fetch: async () => { throw new Error("unexpected fetch"); } });
  assert.equal(await actions.run({}, { operation: "save", target: {}, name: "file.txt" }), false);
});

test("missing versions propagate a useful backend reason and do not open current bytes", async () => {
  let opened = false;
  const actions = createFileActions({ shell: { openPath: async () => { opened = true; } }, dialog: {},
    BrowserWindow: { fromWebContents: () => ({}) }, apiBase: () => "http://local", session: () => "s",
    fetch: async () => Response.json({ detail: "磁盘文件已变化，此历史版本未保存正文" }, { status: 409 }) });
  await assert.rejects(actions.run({}, { operation: "open", target: { loop_id: "loop" }, name: "report.pdf" }), /历史版本未保存正文/);
  assert.equal(opened, false);
});

test("HTML preview opens an isolated window with no desktop bridge or active scripts", async () => {
  let options, loaded, navigation;
  class PreviewWindow {
    static fromWebContents() { return {}; }
    constructor(value) {
      options = value;
      this.webContents = { setWindowOpenHandler: fn => assert.deepEqual(fn(), { action: "deny" }), on: (_event, fn) => { navigation = fn; } };
    }
    async loadURL(url) { loaded = url; }
  }
  const actions = createFileActions({ shell: {}, dialog: {}, BrowserWindow: PreviewWindow,
    apiBase: () => "http://local", session: () => "s", fetch: async () => new Response('<h1>Report</h1><script>alert(1)</script>') });
  await actions.run({}, { operation: "html", target: { task_id: "task", content_ref: "report.html" }, name: "report.html" });
  assert.equal(options.webPreferences.nodeIntegration, false);
  assert.equal(options.webPreferences.javascript, false);
  assert.equal(options.webPreferences.preload, undefined);
  assert.match(decodeURIComponent(loaded), /Content-Security-Policy/);
  let blocked = false; navigation({ preventDefault: () => { blocked = true; } });
  assert.equal(blocked, true);
});
