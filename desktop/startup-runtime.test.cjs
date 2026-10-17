const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { runCommand, createStartupRuntime, createStageRecorder } = require("./startup-runtime.cjs");

async function fixture(t, statuses = ["current"], externalDatabase = false) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "focus-runtime-"));
  t.after(() => fs.rm(home, { recursive: true, force: true }));
  const rootDir = path.join(home, "app");
  await fs.mkdir(path.join(rootDir, "desktop"), { recursive: true });
  await fs.writeFile(path.join(rootDir, "desktop/package.json"), "{}");
  const executable = path.join(home, "python.exe");
  await fs.writeFile(executable, "fixture Python");
  const calls = [], entries = [];
  const recorder = createStageRecorder({ report: entry => entries.push(entry) });
  const controller = new AbortController();
  const run = async (command, args, options) => {
    options.signal?.throwIfAborted();
    calls.push({ command, args, options });
    if (args.some(arg => arg.endsWith("database-state.py"))) return JSON.stringify({ status: statuses.length > 1 ? statuses.shift() : statuses[0] });
    if (args.includes("-c")) return executable;
    return "fixture";
  };
  const runtime = createStartupRuntime({ rootDir, home, python: executable, externalDatabase, signal: controller.signal, run, stage: recorder.stage });
  return { runtime, calls, entries, rootDir, executable, controller };
}

test("daily start reuses verified runtime and checks real database without Compose or migrations", async t => {
  const { runtime, calls, entries } = await fixture(t);
  const python = await runtime.prepare();
  assert.equal(calls.length, 3);
  calls.length = 0;
  assert.deepEqual(await runtime.prepare(), python);
  await runtime.prepareDatabase(python, { FOCUS_DATABASE_URL: "fixture" });
  assert.equal(calls.length, 1);
  assert.ok(calls[0].args[0].endsWith("database-state.py"));
  assert.ok(entries.every(entry => entry.status === "ok" && entry.duration_ms >= 0));
});

test("changed dependencies, replaced interpreter and corrupt cache repeat preparation", async t => {
  const { runtime, calls, rootDir, executable } = await fixture(t);
  await runtime.prepare();
  calls.length = 0;
  await fs.writeFile(path.join(rootDir, "desktop/package.json"), '{"version":"2"}');
  await runtime.prepare();
  assert.equal(calls.length, 3);
  calls.length = 0;
  await fs.writeFile(executable, "replaced interpreter");
  await runtime.prepare();
  assert.equal(calls.length, 3);
  calls.length = 0;
  await fs.writeFile(runtime.cachePath, "broken");
  await runtime.prepare();
  assert.equal(calls.length, 3);
});

test("install preparation stays valid when npm start adds PATH entries", async t => {
  const { runtime, calls } = await fixture(t);
  await runtime.prepare(true);
  calls.length = 0;
  const previousPath = process.env.PATH;
  try {
    process.env.PATH = path.join(os.tmpdir(), "node_modules/.bin") + path.delimiter + previousPath;
    await runtime.prepare();
    assert.equal(calls.length, 0);
  } finally { process.env.PATH = previousPath; }
});

test("quitting also rejects a prepared cached runtime", async t => {
  const { runtime, controller } = await fixture(t);
  await runtime.prepare();
  controller.abort(new Error("quit"));
  await assert.rejects(runtime.prepare(), /quit/);
});

test("unavailable external database never starts the local Compose service", async t => {
  const { runtime, calls } = await fixture(t, ["unavailable"], true);
  const python = await runtime.prepare();
  calls.length = 0;
  await assert.rejects(runtime.prepareDatabase(python, {}), /配置的数据库无法连接/);
  assert.equal(calls.some(call => call.command === "docker"), false);
});

test("stopped data service is started once; only pending schema is migrated and verified", async t => {
  const { runtime, calls } = await fixture(t, ["unavailable", "pending", "current"]);
  const python = await runtime.prepare();
  calls.length = 0;
  await runtime.prepareDatabase(python, {});
  assert.equal(calls.filter(call => call.command === "docker").length, 1);
  assert.equal(calls.filter(call => call.args.includes("upgrade")).length, 1);
  assert.equal(calls.filter(call => call.args[0].endsWith("database-state.py")).length, 3);
  assert.ok(calls.every(call => call.options.signal));
});

test("database is checked again on every start, even with an unchanged runtime", async t => {
  const { runtime, calls } = await fixture(t, ["current", "pending", "current"]);
  const python = await runtime.prepare();
  await runtime.prepareDatabase(python, {});
  calls.length = 0;
  await runtime.prepareDatabase(await runtime.prepare(), {});
  assert.equal(calls.filter(call => call.args.includes("upgrade")).length, 1);
});

test("a migration that fails to reach head prevents gateway startup", async t => {
  const { runtime } = await fixture(t, ["pending"]);
  await assert.rejects(runtime.prepareDatabase(await runtime.prepare(), {}), /版本仍未就绪/);
});

test("quitting cancels preparation without writing a success cache", async t => {
  const { runtime, controller } = await fixture(t);
  controller.abort(new Error("quit"));
  await assert.rejects(runtime.prepare(), /quit/);
  await assert.rejects(fs.access(runtime.cachePath));
});

test("real subprocess remains asynchronous, times out, and hides command arguments in errors", async () => {
  let ticked = false;
  const tick = setTimeout(() => { ticked = true; }, 10);
  await assert.rejects(runCommand(process.execPath, ["-e", "setTimeout(()=>{}, 10000)", "private-token"], { timeout: 150, label: "测试阶段" }), error => {
    assert.match(error.message, /测试阶段超过/);
    assert.doesNotMatch(error.message, /private-token/);
    return true;
  });
  clearTimeout(tick);
  assert.equal(ticked, true);
});

test("real subprocess is killed on cancellation", async () => {
  const controller = new AbortController();
  const reason = new Error("quit");
  const running = runCommand(process.execPath, ["-e", "setTimeout(()=>{},10000)"], { signal: controller.signal });
  setTimeout(() => controller.abort(reason), 50);
  await assert.rejects(running, error => error === reason);
});

test("failed stage records elapsed and failure status", async () => {
  const recorder = createStageRecorder();
  await assert.rejects(recorder.stage("migration", "迁移", 40, async () => { throw new Error("fixture"); }));
  assert.equal(recorder.entries[0].status, "failed");
  assert.ok(recorder.entries[0].elapsed_ms >= recorder.entries[0].duration_ms);
});
