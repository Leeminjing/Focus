/* Opt-in destructive recovery drill against a new, disposable container only.
 * FOCUS_UPDATE_POSTGRES_TEST=1 node --test desktop/tests/integration/managed-update-postgres.test.cjs */
"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const cp = require("node:child_process");
const { setTimeout: delay } = require("node:timers/promises");
const { Installation } = require("../../../scripts/managed-update.cjs");

test("real PostgreSQL migration, Gateway health and complete Context/Revision/run recovery", {
  skip: process.env.FOCUS_UPDATE_POSTGRES_TEST !== "1", timeout: 180000,
}, async t => {
  const name = `focus-update-drill-${process.pid}-${Date.now()}`;
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "focus-postgres-drill-"));
  const docker = (args, options = {}) => cp.execFileSync("docker", args, { encoding: "utf8", windowsHide: true, ...options });
  t.after(() => {
    docker(["rm", "-f", "-v", name]);
    fs.rmSync(home, { recursive: true, force: true });
  });
  docker(["run", "-d", "--name", name, "-e", "POSTGRES_USER=focus", "-e", "POSTGRES_PASSWORD=drill-only",
    "-e", "POSTGRES_DB=focus", "-p", "127.0.0.1::5432", "pgvector/pgvector:pg17"]);
  let ready = false;
  for (let attempt = 0; attempt < 60; attempt++) {
    try { docker(["exec", name, "pg_isready", "-U", "focus", "-d", "focus"], { stdio: "pipe" }); ready = true; break; }
    catch { await delay(500); }
  }
  assert.ok(ready, "isolated PostgreSQL must become ready");
  const port = docker(["port", name, "5432/tcp"]).trim().split(":").at(-1);
  const i = new Installation(home, path.resolve(__dirname, "../../.."), process.env.FOCUS_TEST_PYTHON || "python");
  Object.assign(i.env, { FOCUS_DATABASE_URL: `postgresql+asyncpg://focus:drill-only@127.0.0.1:${port}/focus`,
    OPENAI_API_KEY: "drill-placeholder", JWT_SECRET: "drill-unused", FOCUS_DESKTOP_SESSION: "drill-session" });
  delete i.env.FOCUS_MODEL;
  // Keep the real dump/restore implementation; redirect its container transport
  // to the exact disposable container, never the user's desktop Compose DB.
  i.compose = (args, options = {}) => {
    assert.deepEqual(args.slice(0, 3), ["exec", "-T", "postgres"]);
    return docker(["exec", "-i", name, ...args.slice(3)], options);
  };
  const sql = statement => docker(["exec", name, "psql", "-U", "focus", "-d", "focus", "-v", "ON_ERROR_STOP=1", "-Atc", statement]).trim();
  i.migrate();
  sql(`INSERT INTO desktop_workspaces(workspace_id,path,display_name) VALUES ('drill-ws','/drill/workspace','Recovery drill');
    INSERT INTO desktop_threads(task_id,workspace_id,thread_id,title) VALUES ('drill-context','drill-ws','drill-execution','User work');
    INSERT INTO desktop_context_revisions(revision_id,context_id,generation,execution_thread_id,checkpoint_id,payload_mode,content_hash,projection_status,origin_kind,authored_messages)
      VALUES ('drill-revision','drill-context',1,'drill-execution','checkpoint-1','checkpoint',repeat('a',64),'valid','root','[{"role":"human","content":"keep my work"}]');
    UPDATE desktop_threads SET current_revision_id='drill-revision' WHERE task_id='drill-context';
    INSERT INTO desktop_runs(run_id,task_id,agent_id,kind,status,input_messages) VALUES
      ('drill-run','drill-context','main:drill-context','main','completed','[]'),
      ('drill-live-run','drill-context','main:drill-context','main','running','[]');`);
  const snapshot = () => sql(`SELECT json_build_array(
    (SELECT row_to_json(t) FROM desktop_threads t WHERE task_id='drill-context'),
    (SELECT row_to_json(r) FROM desktop_context_revisions r WHERE revision_id='drill-revision'),
    (SELECT json_agg(h ORDER BY run_id) FROM desktop_runs h WHERE task_id='drill-context'))`);
  const before = snapshot();
  i.health();
  assert.equal(snapshot(), before, "acceptance must not resume or modify existing work");
  const directory = path.join(home, "backup"); fs.mkdirSync(directory);
  const backupSha256 = i.backup(directory);
  // The title column now has a notification trigger. Remove its dependants in
  // this disposable DB too, so the deliberately broken migration can execute.
  sql("CREATE TABLE failed_migration_residue(id int); UPDATE desktop_context_revisions SET authored_messages='[]'; DELETE FROM desktop_runs; ALTER TABLE desktop_threads DROP COLUMN title CASCADE");
  const run = i.run.bind(i);
  i.run = (command, args, options) => run(command, args, { ...options, stdio: "pipe" });
  assert.throws(() => i.health(), /Command failed/, "a stamped but incompatible schema must fail health checks");
  i.run = run;
  i.restore(directory, { backupSha256 });
  assert.equal(snapshot(), before, "Context, immutable Revision and run history must survive whole-database restore");
  assert.equal(sql("SELECT to_regclass('failed_migration_residue') IS NULL"), "t", "new tables from a failed migration must disappear");
  i.health();
  fs.appendFileSync(path.join(directory, "database.dump"), "corruption");
  assert.throws(() => i.restore(directory, { backupSha256 }), /checksum mismatch/);
  assert.equal(snapshot(), before, "damaged backups must be rejected before touching the database");
});
