"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { inventory, cleanup, backup } = require("../scripts/storage-maintenance.cjs");
const { Installation, atomicJson, withUpdateLock } = require("../scripts/managed-update.cjs");
const policy = { backup_keep_count: 1, backup_retention_days: 1 };

function fixture(t) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "focus-storage-"));
  t.after(() => fs.rmSync(home, { recursive: true, force: true }));
  const add = (kind, stamp, complete = true) => {
    const id = `${kind}-${stamp}-aabbccdd`, directory = path.join(home, "backups", id);
    fs.mkdirSync(directory, { recursive: true });
    fs.writeFileSync(path.join(directory, "database.dump"), "database contents");
    if (complete && kind === "regular") atomicJson(path.join(directory, "backup.json"), { schema: 1, id, created_at: new Date(stamp).toISOString(), sha256: "a".repeat(64) });
    if (complete && kind === "update") {
      atomicJson(path.join(directory, "recovery.json"), { id, backupSha256: "a".repeat(64) });
      fs.writeFileSync(path.join(directory, "dependencies.json"), "{}");
      fs.writeFileSync(path.join(directory, "recover.cjs"), "// recovery");
    }
    return id;
  };
  return { home, add };
}

test("retention combines age, per-type count, current recovery point and incomplete protection", t => {
  const { home, add } = fixture(t);
  const stamp = Date.now() - 10 * 86400000;
  const obsolete = add("regular", stamp), newest = add("regular", stamp + 1);
  const accepted = add("update", stamp), latestUpdate = add("update", stamp + 1);
  const incomplete = add("regular", stamp + 2, false);
  atomicJson(path.join(home, "install.json"), { recoveryPoint: path.join(home, "backups", accepted) });
  const rows = inventory(home, policy).entries;
  assert.equal(rows.find(r => r.id === obsolete).protected_reason, null);
  assert.equal(rows.find(r => r.id === newest).protected_reason, "recent_backup");
  assert.equal(rows.find(r => r.id === accepted).protected_reason, "accepted_recovery_point");
  assert.equal(rows.find(r => r.id === latestUpdate).protected_reason, "recent_backup");
  assert.equal(rows.find(r => r.id === incomplete).protected_reason, "unrecognised_or_incomplete");
  assert.deepEqual(cleanup(home, policy, rows.map(r => r.id)).removed, [obsolete]);
  assert.ok(fs.existsSync(path.join(home, "backups", accepted)));
  assert.deepEqual(cleanup(home, policy, ["../../outside"]).removed, []);
  assert.throws(() => cleanup(home, {}, [newest]), /Invalid backup retention policy/);
});

test("a newly pending recovery invalidates a previously eligible deletion", t => {
  const { home, add } = fixture(t);
  const old = add("update", Date.now() - 10 * 86400000);
  add("update", Date.now());
  assert.equal(inventory(home, policy).entries.find(r => r.id === old).protected_reason, null);
  atomicJson(path.join(home, "update-state.json"), { id: old });
  assert.deepEqual(cleanup(home, policy, [old]).removed, []);
  assert.equal(inventory(home, policy).entries.find(r => r.id === old).protected_reason, "active_recovery");
  fs.writeFileSync(path.join(home, "update-state.json"), "invalid JSON");
  assert.throws(() => cleanup(home, policy, [old]));
});

test("links in regular backups remain protected and are never followed or deleted", t => {
  const { home, add } = fixture(t);
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), "focus-storage-outside-"));
  t.after(() => fs.rmSync(outside, { recursive: true, force: true }));
  fs.writeFileSync(path.join(outside, "keep"), "external");
  const old = add("regular", Date.now() - 10 * 86400000);
  add("regular", Date.now());
  fs.symlinkSync(outside, path.join(home, "backups", old, "linked"), process.platform === "win32" ? "junction" : "dir");
  assert.equal(inventory(home, policy).entries.find(r => r.id === old).protected_reason, "linked_or_not_directory");
  assert.deepEqual(cleanup(home, policy, [old]).removed, []);
  assert.equal(fs.readFileSync(path.join(outside, "keep"), "utf8"), "external");
});

test("regular backup is published only after verification; failures stay protected", async t => {
  const { home } = fixture(t);
  const i = { home, prepareDatabase() {}, git: () => "f".repeat(40), backup(directory) {
    fs.writeFileSync(path.join(directory, "database.dump"), "archive"); return "a".repeat(64);
  } };
  const result = await withUpdateLock(home, () => backup(i));
  assert.equal(result.scope, "postgresql");
  assert.equal(inventory(home, policy).entries[0].kind, "regular");
  i.backup = () => { throw Error("verification failed"); };
  assert.throws(() => backup(i), /verification failed/);
  assert.ok(inventory(home, policy).entries.some(row => row.protected_reason === "unrecognised_or_incomplete"));
  atomicJson(path.join(home, "update-state.json"), { id: "pending" });
  assert.throws(() => backup(i), /Finish Focus recovery/);
});

test("update dependency snapshots with links remain eligible and cleanup preserves link targets", t => {
  const { home, add } = fixture(t);
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), "focus-dependency-outside-"));
  t.after(() => fs.rmSync(outside, { recursive: true, force: true }));
  fs.writeFileSync(path.join(outside, "keep"), "external dependency");
  const appDir = path.join(home, "app");
  const modules = path.join(appDir, "desktop/node_modules");
  const venv = path.join(home, "runtime/venv");
  for (const directory of [modules, venv]) fs.mkdirSync(directory, { recursive: true });
  fs.writeFileSync(path.join(modules, "package"), "saved dependency");
  fs.symlinkSync(outside, path.join(venv, "linked"), process.platform === "win32" ? "junction" : "dir");
  const old = add("update", Date.now() - 10 * 86400000);
  add("update", Date.now());
  const installation = new Installation(home, appDir, "python");
  installation.run = () => "fixture==1\n";
  installation.snapshotDependencies(path.join(home, "backups", old));
  // Windows cp copies junction contents; put a link in the snapshot to exercise
  // the preserved links produced by cp on macOS/Linux on every test platform.
  if (process.platform === "win32") {
    fs.symlinkSync(outside, path.join(home, "backups", old, "venv/preserved-link"), "junction");
  }
  const snapshot = path.join(home, "backups", old);
  const linkType = process.platform === "win32" ? "junction" : "dir";
  fs.symlinkSync(path.join(outside, "missing"), path.join(snapshot, "node_modules/dangling"), linkType);
  fs.symlinkSync(path.join(snapshot, "venv"), path.join(snapshot, "venv/cycle"), linkType);
  if (process.platform !== "win32") {
    fs.symlinkSync(path.join(outside, "keep"), path.join(snapshot, "venv/python"));
    fs.symlinkSync("missing-python", path.join(snapshot, "venv/python3"));
  }
  const row = inventory(home, policy).entries.find(row => row.id === old);
  assert.equal(row.kind, "update", "含依赖链接的完整更新快照仍被识别");
  assert.equal(row.protected_reason, null, "过期依赖快照可以进入清理预览");
  assert.deepEqual(cleanup(home, policy, [old]), { removed: [old], errors: [] });
  assert.equal(fs.existsSync(path.join(home, "backups", old)), false);
  assert.equal(fs.readFileSync(path.join(outside, "keep"), "utf8"), "external dependency");
  assert.equal(fs.readFileSync(path.join(modules, "package"), "utf8"), "saved dependency");
});

test("linked backup containers and recovery point roots stay protected", t => {
  const { home, add } = fixture(t);
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), "focus-linked-recovery-"));
  t.after(() => fs.rmSync(outside, { recursive: true, force: true }));
  fs.writeFileSync(path.join(outside, "keep"), "external recovery point");
  const old = add("update", Date.now() - 10 * 86400000);
  add("update", Date.now());
  const linkType = process.platform === "win32" ? "junction" : "dir";
  const root = path.join(home, "backups");
  const target = path.join(root, old);
  assert.equal(inventory(home, policy).entries.find(row => row.id === old).protected_reason, null);
  // Replace the previously previewed recovery directory with a link.
  assert.equal(path.dirname(target), root);
  fs.renameSync(target, path.join(home, "saved-recovery"));
  fs.symlinkSync(outside, target, linkType);
  assert.equal(inventory(home, policy).entries.find(row => row.id === old).protected_reason, "linked_or_not_directory");
  assert.deepEqual(cleanup(home, policy, [old]), { removed: [], errors: [] });
  fs.renameSync(root, path.join(home, "saved-backups"));
  fs.symlinkSync(outside, root, linkType);
  assert.throws(() => inventory(home, policy), /Backup directory must not be a link/);
  assert.throws(() => cleanup(home, policy, [old]), /Backup directory must not be a link/);
  assert.equal(fs.readFileSync(path.join(outside, "keep"), "utf8"), "external recovery point");
});

test("only links inside real update dependency directories are allowed", t => {
  const { home, add } = fixture(t);
  const outside = path.join(home, "external");
  fs.mkdirSync(outside);
  fs.writeFileSync(path.join(outside, "keep"), "external");
  const stamp = Date.now() - 10 * 86400000;
  const linkType = process.platform === "win32" ? "junction" : "dir";
  const unsafe = ["venv", "node_modules", "unexpected", "recovery.json", "database.dump", "dependencies.json", "recover.cjs"];
  const ids = unsafe.map((name, index) => {
    const id = add("update", stamp + index);
    const link = path.join(home, "backups", id, name);
    if (fs.existsSync(link)) fs.unlinkSync(link);
    fs.symlinkSync(outside, link, linkType);
    return id;
  });
  add("update", Date.now());
  const rows = inventory(home, policy).entries;
  for (const id of ids) assert.equal(rows.find(row => row.id === id).protected_reason, "linked_or_not_directory");
  assert.deepEqual(cleanup(home, policy, ids), { removed: [], errors: [] });
  assert.equal(fs.readFileSync(path.join(outside, "keep"), "utf8"), "external");
});

test("dependency links do not bypass accepted, pending, count or age protections", t => {
  const { home, add } = fixture(t);
  const outside = path.join(home, "external");
  fs.mkdirSync(outside);
  const stamp = Date.now() - 10 * 86400000;
  const accepted = add("update", stamp);
  const old = add("update", stamp + 1);
  const recent = add("update", Date.now() - 1000);
  const newest = add("update", Date.now());
  for (const id of [accepted, old, recent, newest]) {
    const snapshot = path.join(home, "backups", id, "venv");
    fs.mkdirSync(snapshot);
    fs.symlinkSync(outside, path.join(snapshot, "python"), process.platform === "win32" ? "junction" : "dir");
  }
  atomicJson(path.join(home, "install.json"), { recoveryPoint: path.join(home, "backups", accepted) });
  const rows = inventory(home, policy).entries;
  assert.equal(rows.find(row => row.id === accepted).protected_reason, "accepted_recovery_point");
  assert.equal(rows.find(row => row.id === old).protected_reason, null);
  assert.equal(rows.find(row => row.id === recent).protected_reason, "within_retention");
  assert.equal(rows.find(row => row.id === newest).protected_reason, "recent_backup");
  assert.deepEqual(cleanup(home, policy, [accepted, recent, newest]), { removed: [], errors: [] });
  atomicJson(path.join(home, "update-state.json"), { id: old });
  const pending = inventory(home, policy).entries;
  assert.equal(pending.find(row => row.id === old).protected_reason, "active_recovery");
  assert.equal(pending.find(row => row.id === newest).protected_reason, "recovery_pending");
  assert.deepEqual(cleanup(home, policy, [accepted, old, recent, newest]), { removed: [], errors: [] });
});
