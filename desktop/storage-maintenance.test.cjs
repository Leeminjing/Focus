"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { inventory, cleanup, backup } = require("../scripts/storage-maintenance.cjs");
const { atomicJson, withUpdateLock } = require("../scripts/managed-update.cjs");
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

test("linked backup roots and linked contents are never followed or deleted", t => {
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
