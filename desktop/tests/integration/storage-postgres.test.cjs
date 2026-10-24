/* Disposable archive creation/restore drill. Opt in with FOCUS_STORAGE_POSTGRES_TEST=1. */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const cp = require("node:child_process");
const { setTimeout: delay } = require("node:timers/promises");
const { Installation, withUpdateLock } = require("../../../scripts/managed-update.cjs");
const { backup, inventory } = require("../../../scripts/storage-maintenance.cjs");

test("regular backup verifies and restores real PostgreSQL content; corrupt archive is rejected", {
  skip: process.env.FOCUS_STORAGE_POSTGRES_TEST !== "1", timeout: 120000,
}, async t => {
  const name = `focus-storage-drill-${process.pid}-${Date.now()}`;
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "focus-storage-drill-"));
  const docker = (args, options = {}) => cp.execFileSync("docker", args, { encoding: "utf8", windowsHide: true, ...options });
  t.after(() => {
    try { docker(["rm", "-f", "-v", name]); }
    finally { fs.rmSync(home, { recursive: true, force: true }); }
  });
  // No host port or desktop volume is used.
  docker(["run", "-d", "--name", name, "-e", "POSTGRES_USER=focus", "-e", "POSTGRES_PASSWORD=drill-only",
    "-e", "POSTGRES_DB=focus", "pgvector/pgvector:pg17"]);
  let ready = false;
  for (let attempt = 0; attempt < 60; attempt++) {
    try { docker(["exec", name, "pg_isready", "-U", "focus", "-d", "focus"], { stdio: "pipe" }); ready = true; break; }
    catch { await delay(500); }
  }
  assert.ok(ready);
  const sql = statement => docker(["exec", name, "psql", "-U", "focus", "-d", "focus", "-v", "ON_ERROR_STOP=1", "-Atc", statement]).trim();
  sql("CREATE TABLE evidence(id integer PRIMARY KEY, body text); INSERT INTO evidence VALUES (1, 'retained content')");
  const i = new Installation(home, path.resolve(__dirname, "../../.."), "python");
  i.prepareDatabase = () => {};
  i.compose = (args, options = {}) => {
    assert.deepEqual(args.slice(0, 3), ["exec", "-T", "postgres"]);
    return docker(["exec", "-i", name, ...args.slice(3)], options);
  };
  const manifest = await withUpdateLock(home, () => backup(i));
  const directory = path.join(home, "backups", manifest.id);
  assert.equal(inventory(home, { backup_keep_count: 1, backup_retention_days: 30 }).entries[0].kind, "regular");
  assert.match(manifest.sha256, /^[a-f0-9]{64}$/);
  sql("DELETE FROM evidence; CREATE TABLE residue(id integer)");
  i.restore(directory, { backupSha256: manifest.sha256 });
  assert.equal(sql("SELECT body FROM evidence WHERE id=1"), "retained content");
  assert.equal(sql("SELECT to_regclass('residue') IS NULL"), "t");
  fs.appendFileSync(path.join(directory, "database.dump"), "corrupted");
  assert.throws(() => i.restore(directory, { backupSha256: manifest.sha256 }), /checksum mismatch/);
  assert.equal(sql("SELECT body FROM evidence WHERE id=1"), "retained content");
});
