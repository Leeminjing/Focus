"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { createElement } = require("./test-dom.cjs");
const { create } = require("./storage-settings.js");
const policy = { journal_retention_days: 7, backup_retention_days: 30, backup_keep_count: 3, automatic_cleanup: false, backup_interval_hours: 0 };
const escapeHtml = value => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;");
const settle = () => new Promise(resolve => setImmediate(resolve));

test("settings preview is explicit, uses saved policy and submits only its token", async () => {
  const host = createElement("section");
  const calls = [];
  const snapshot = { policy: { ...policy }, database_bytes: 2048, journal_bytes: 1024, journal_events: 10,
    backup_available: true, backups: { bytes: 4096, entries: [{ id: "<unsafe>", bytes: 4096, kind: "update", protected_reason: "active_recovery" }] } };
  const api = async (url, options = {}) => {
    calls.push({ url, options });
    if (url.endsWith("/policy")) { snapshot.policy = JSON.parse(options.body); return snapshot.policy; }
    if (url.endsWith("/preview")) return { token: "preview-token", journal_events: 2, backup_ids: ["regular-old"], backup_bytes: 100 };
    if (url.endsWith("/cleanup")) return { journal_events_removed: 1, backups_removed: [] };
    return snapshot;
  };
  const controller = create({ api, host, escapeHtml });
  await controller.load();
  assert.match(host.innerHTML, /正在恢复/);
  assert.doesNotMatch(host.innerHTML, /<unsafe>/);
  assert.match(host.innerHTML, /&lt;unsafe>/);
  const click = async action => {
    const button = host.querySelector(`[data-storage-action="${action}"]`);
    assert.ok(button, action);
    button.dispatchEvent({ type: "click" });
    await settle();
  };
  await click("preview");
  assert.ok(host.querySelector(".storage-preview"));
  assert.equal(calls.filter(row => row.options.method === "PUT").length, 0);
  const input = host.querySelector('[data-storage-field="backup_keep_count"]');
  input.value = "4";
  input.dispatchEvent({ type: "input" });
  assert.equal(host.querySelector(".storage-preview"), null);
  await click("preview");
  assert.match(host.innerHTML, /请先保存策略/);
  await click("save");
  assert.equal(snapshot.policy.backup_keep_count, 4);
  await click("preview");
  await click("cleanup");
  const call = calls.find(row => row.url.endsWith("/cleanup"));
  assert.deepEqual(JSON.parse(call.options.body), { token: "preview-token" });
  assert.equal(host.querySelector(".storage-preview"), null);
  assert.match(host.innerHTML, /已清理 1 条日志、0 个备份/);
});

test("initial API errors remain visible with a retry action", async () => {
  const host = createElement("section");
  const controller = create({ host, escapeHtml, english: () => true, api: async () => { throw Error("fixture unavailable"); } });
  await controller.load();
  assert.match(host.innerHTML, /fixture unavailable/);
  assert.match(host.innerHTML, /Retry/);
});
