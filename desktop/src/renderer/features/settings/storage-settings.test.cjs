"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { createElement } = require("../../../../tests/helpers/test-dom.cjs");
const { create } = require("./storage-settings.js");
const policy = { journal_retention_days: 7, backup_retention_days: 30, backup_keep_count: 3, automatic_cleanup: false, backup_interval_hours: 0 };
const escapeHtml = value => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;");
const settle = () => new Promise(resolve => setImmediate(resolve));

async function fixture(request = () => undefined) {
  const host = createElement("section"), calls = [];
  const snapshot = { policy: { ...policy }, database_bytes: 2048, journal_bytes: 1024, journal_events: 10,
    backup_available: true, backups: { bytes: 4096, entries: [] } };
  const controller = create({ host, escapeHtml, api: async (url, options = {}) => {
    calls.push({ url, options });
    const result = await request(url, options);
    if (result !== undefined) return result;
    if (url.endsWith("/policy")) { snapshot.policy = JSON.parse(options.body); return snapshot.policy; }
    if (url.endsWith("/preview")) return { token: "bound-token", expires_in_seconds: 600, journal_events: 2, backup_ids: ["old"], backup_bytes: 100 };
    if (url.endsWith("/cleanup")) return { journal_events_removed: 1, backups_removed: [], errors: [] };
    if (url.endsWith("/backup")) return { id: "new-backup" };
    return structuredClone(snapshot);
  } });
  await controller.load();
  const click = async action => {
    host.querySelector(`[data-storage-action="${action}"]`).dispatchEvent({ type: "click" });
    await settle();
  };
  return { host, calls, snapshot, controller, click };
}

test("capacity nests Journal within the database and exposes separate policy, backup and cleanup regions", async () => {
  const f = await fixture();
  const usage = f.host.querySelector(".storage-usage");
  assert.equal(usage.querySelectorAll(":scope > div").length, 2);
  assert.match(usage.querySelector(".storage-usage-children").textContent, /其中 Journal 日志1.0 KiB · 10 条/);
  assert.equal(f.host.querySelectorAll(".storage-section").length, 4);
  assert.match(f.host.querySelector(".storage-backup-scope").textContent, /不包含工作区文件、升级依赖快照/);
  const steps = f.host.querySelector(".storage-cleanup-steps");
  assert.equal(steps.querySelectorAll(":scope > li").length, 2);
  assert.ok(f.host.querySelector('[data-storage-action="cleanup"]').disabled);
  await f.click("cleanup");
  assert.equal(f.calls.filter(row => row.url.endsWith("/cleanup")).length, 0);
});

test("editing each policy field removes the preview and disables execution without replacing the input", async () => {
  for (const key of Object.keys(policy)) {
    const f = await fixture();
    await f.click("preview");
    const input = f.host.querySelector(`[data-storage-field="${key}"]`);
    if (key === "automatic_cleanup") input.checked = true;
    else input.value = String(policy[key] + 1);
    input.dispatchEvent({ type: "input" });
    assert.equal(f.host.querySelector(`[data-storage-field="${key}"]`), input);
    assert.equal(f.host.querySelector(".storage-preview"), null);
    assert.ok(f.host.querySelector('[data-storage-action="cleanup"]').disabled);
    await f.click("cleanup");
    assert.equal(f.calls.filter(row => row.url.endsWith("/cleanup")).length, 0);
    await f.click("preview");
    assert.equal(f.calls.filter(row => row.url.endsWith("/preview")).length, 1, "unsaved edits cannot produce a new token");
  }
});

test("expired tokens are discarded and partial execution reports actual work separately from the preview", async () => {
  let expired = true;
  const f = await fixture(url => {
    if (!url.endsWith("/cleanup")) return;
    if (expired) throw Error("预览已过期，请重新预览");
    return { journal_events_removed: 1, backups_removed: [], errors: ["备份受保护，未删除"] };
  });
  await f.click("preview");
  await f.click("cleanup");
  assert.equal(f.host.querySelector(".storage-preview"), null);
  assert.ok(f.host.querySelector('[data-storage-action="cleanup"]').disabled);
  assert.match(f.host.querySelector('[data-storage-status="cleanup"]').textContent, /预览已过期/);
  expired = false;
  await f.click("preview");
  await f.click("cleanup");
  const result = f.host.querySelector('[data-storage-status="cleanup"]');
  assert.match(result.textContent, /已清理 1 条日志、0 个备份；备份受保护，未删除/);
  assert.ok(result.classList.contains("danger"));
  assert.deepEqual(f.calls.filter(row => row.url.endsWith("/cleanup")).map(row => JSON.parse(row.options.body)), [{ token: "bound-token" }, { token: "bound-token" }]);
});

test("capacity refresh failures do not replace backup and cleanup results", async () => {
  let failRefresh = false;
  const f = await fixture(url => {
    if (url.endsWith("/storage") && failRefresh) throw Error("容量暂不可用");
  });
  await f.click("preview");
  failRefresh = true;
  await f.click("cleanup");
  assert.match(f.host.querySelector('[data-storage-status="cleanup"]').textContent, /已清理 1 条日志、0 个备份/);
  assert.match(f.host.querySelector('[data-storage-status="refresh"]').textContent, /容量暂不可用/);
  await f.click("backup");
  assert.match(f.host.querySelector('[data-storage-status="backup"]').textContent, /数据库备份已完成：new-backup/);
  assert.match(f.host.querySelector('[data-storage-status="cleanup"]').textContent, /已清理/);
});

test("a failed policy save discards both the token and its previous ready-to-execute feedback", async () => {
  const f = await fixture(url => { if (url.endsWith("/policy")) throw Error("策略保存失败"); });
  await f.click("preview");
  await f.click("save");
  assert.equal(f.host.querySelector(".storage-preview"), null);
  assert.equal(f.host.querySelector('[data-storage-status="preview"]').textContent, "");
  assert.match(f.host.querySelector('[data-storage-status="save"]').textContent, /策略保存失败/);
  assert.ok(f.host.querySelector('[data-storage-action="cleanup"]').disabled);
  await f.click("cleanup");
  assert.equal(f.calls.filter(row => row.url.endsWith("/cleanup")).length, 0);
});

test("a policy changed on refresh invalidates the preview and an empty preview cannot execute", async () => {
  const f = await fixture();
  await f.click("preview");
  f.snapshot.policy.backup_keep_count += 1;
  await f.controller.load();
  assert.equal(f.host.querySelector(".storage-preview"), null);
  assert.ok(f.host.querySelector('[data-storage-action="cleanup"]').disabled);
  const empty = await fixture(url => url.endsWith("/preview")
    ? { token: "empty", journal_events: 0, backup_ids: [], backup_bytes: 0 } : undefined);
  await empty.click("preview");
  assert.ok(empty.host.querySelector('[data-storage-action="cleanup"]').disabled);
  assert.match(empty.host.querySelector('[data-storage-status="preview"]').textContent, /没有可清理项目/);
});

test("settings preview is explicit, uses saved policy and submits only its token", async () => {
  const { host, calls, snapshot, controller, click } = await fixture();
  snapshot.backups.entries = [{ id: "<unsafe>", bytes: 4096, kind: "update", protected_reason: "active_recovery" }];
  await controller.load();
  assert.match(host.innerHTML, /正在恢复/);
  assert.doesNotMatch(host.innerHTML, /<unsafe>/);
  assert.match(host.innerHTML, /&lt;unsafe>/);
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
  assert.deepEqual(JSON.parse(call.options.body), { token: "bound-token" });
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
