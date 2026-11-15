/* The remaining editor bridge saves documents and reports dirty events. */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require("node:path").join(__dirname, "onlyoffice-focus-bridge/code.js"), "utf8");

async function run(action, saveAvailable = true) {
  const requests = [];
  const events = {};
  let saves = 0;
  const plugin = {
    attachEditorEvent: (name, handler) => { events[name] = handler; },
    callCommand: (call, _close, _calc, complete) => complete(call()),
  };
  const sandbox = {
    location: { origin: "http://bridge", pathname: "/plugin-config/session/plugin/token/index.html" },
    Asc: { scope: {}, plugin },
    Api: saveAvailable ? { Save: () => { saves++; } } : {},
    addEventListener: (name, handler) => { events[name] = handler; },
    fetch: async (url, options) => {
      requests.push({ url, body: options?.body && JSON.parse(options.body) });
      if (url.includes("/result?")) events.beforeunload();
      return { ok: true, json: async () => options ? {} : { command: { action, command_id: "command", expected_version: 1 } } };
    },
    setTimeout,
  };
  sandbox.window = sandbox;
  vm.runInNewContext(source, sandbox);
  plugin.init();
  events.onDocumentContentChanged();
  for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve));
  assert.ok(requests.every(request => request.url.includes("access_token=token")));
  assert.ok(requests.some(request => request.body?.event === "editorReady"));
  assert.ok(requests.some(request => request.body?.event === "documentChanged" && request.body.document_version === 2));
  assert.equal(events.onTargetPositionChanged, undefined);
  return { saves, result: requests.find(request => request.url.includes("/result?")).body };
}

(async () => {
  const saved = await run("force_save");
  assert.equal(saved.saves, 1);
  assert.equal(saved.result.evidence.force_save_requested, true);
  const unsupported = await run("edit");
  assert.equal(unsupported.saves, 0);
  assert.match(unsupported.result.error, /Unsupported/);
  const missingApi = await run("force_save", false);
  assert.equal(missingApi.saves, 0);
  assert.match(missingApi.result.error, /unavailable/);
  console.log("save-bridge: save, dirty events and unsupported command rejection passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
