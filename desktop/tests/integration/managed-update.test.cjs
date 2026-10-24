"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { Installation, update, stableRelease, assertReady, atomicJson, withUpdateLock, installedChannel, channelOption } = require("../../../scripts/managed-update.cjs");

const oldCommit = "a".repeat(40), newCommit = "b".repeat(40);
const repository = "https://github.com/Leeminjing/Focus.git";
function fixture(t, failure, channel = "stable") {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "focus-release-test-"));
  t.after(() => fs.rmSync(home, { recursive: true, force: true }));
  const appDir = path.join(home, "app");
  fs.mkdirSync(appDir);
  const metadata = { managed: true, repository, appPath: appDir, channel, validatedCommit: oldCommit, ...(channel === "stable" ? { release: "v0.1.0" } : {}) };
  atomicJson(path.join(home, "install.json"), metadata);
  const original = { context: "user work", revision: "immutable history", runs: ["previous result"] };
  let db = structuredClone(original), head = oldCommit, failed = false;
  const calls = [];
  const refs = [];
  let cli = { generation: "cli-old" };
  const hit = step => {
    calls.push(step);
    if (step === failure && !failed) { failed = true; throw new Error(`fixture ${step} failure`); }
  };
  const i = {
    home, appDir,
    assertStopped() {},
    ensurePython() {},
    git(...args) {
      if (args[0] === "remote") return repository;
      if (args[0] === "status") return "";
      if (args[0] === "fetch") { refs.push(args.at(-1)); hit("fetch"); return ""; }
      if (args[0] === "rev-parse") return args[1] === "HEAD" ? head : newCommit;
      if (args[0] === "reset") { head = args[2]; hit(head === oldCommit ? "reset-old" : "reset-new"); return ""; }
      throw Error(args.join(" "));
    },
    prepareDatabase() { hit("prepare"); },
    snapshotDependencies() { hit("snapshot"); },
    restoreDependencies() { hit("restore-dependencies"); },
    backup(directory) { hit("backup"); fs.writeFileSync(path.join(directory, "database.dump"), JSON.stringify(db)); return "checksum"; },
    restore(directory) { hit("restore"); db = JSON.parse(fs.readFileSync(path.join(directory, "database.dump"))); },
    sync() { hit("sync-new"); },
    migrate() { db.revision = "partially migrated"; db.newTable = "must disappear on rollback"; hit("migrate"); },
    health() { hit(head === oldCommit ? "health-old" : "health-new"); },
    snapshotCli() { return cli; },
    restoreCli(value) { cli = value; },
    publishCli() { cli = { generation: "cli-new" }; hit("publish"); },
  };
  return { i, calls, refs, original, metadata, cli: () => cli, database: () => db, head: () => head,
    metadataNow: () => JSON.parse(fs.readFileSync(path.join(home, "install.json"))),
    pending: () => fs.existsSync(path.join(home, "update-state.json")) };
}

test("only published stable releases are eligible; no main fallback", async () => {
  for (const release of [
    { draft: true }, { prerelease: true }, { tag_name: "main" }, { tag_name: "v1.0.0-rc.1" }, { published_at: null },
  ]) {
    await assert.rejects(stableRelease(async () => ({ ok: true, json: async () => ({
      tag_name: "v1.0.0", draft: false, prerelease: false, published_at: "today", ...release,
    }) })), /published stable/);
  }
  await assert.rejects(stableRelease(async () => ({ ok: false, status: 404 })), /No stable release/);
  assert.equal(await stableRelease(async () => ({ ok: true, json: async () => ({
    tag_name: "v1.0.0", draft: false, prerelease: false, published_at: "today",
  }) })), "v1.0.0");
});

test("success requires a recovery point, dependency sync, migration, health, then acceptance", async t => {
  const f = fixture(t);
  await update(f.i, async () => "v0.2.0");
  assert.deepEqual(f.calls, ["fetch", "prepare", "snapshot", "backup", "reset-new", "sync-new", "migrate", "health-new", "publish"]);
  assert.equal(f.metadataNow().validatedCommit, newCommit);
  assert.equal(f.metadataNow().release, "v0.2.0");
  assert.equal(f.pending(), false);
  assert.ok(fs.existsSync(path.join(f.metadataNow().recoveryPoint, "recover.cjs")));
});

for (const channel of ["dev", "stable"]) for (const stage of ["fetch", "prepare", "snapshot", "backup", "reset-new", "sync-new", "migrate", "health-new", "publish"]) {
  test(`${channel} failure at ${stage} never reports acceptance and preserves previous work`, async t => {
    const f = fixture(t, stage, channel);
    await assert.rejects(update(f.i, async () => "v0.2.0"), new RegExp(`fixture ${stage} failure`));
    assert.equal(f.head(), oldCommit);
    assert.deepEqual(f.database(), f.original);
    assert.deepEqual(f.metadataNow(), f.metadata);
    assert.equal(f.pending(), false);
    assert.deepEqual(f.cli(), { generation: "cli-old" });
    if (f.calls.includes("reset-new")) assert.ok(f.calls.includes("restore") && f.calls.includes("health-old"));
  });
}

test("failed recovery blocks startup; retry restores the saved pair before any network access", async t => {
  const f = fixture(t, "health-new");
  const restore = f.i.restore;
  f.i.restore = () => { throw Error("restore unavailable"); };
  await assert.rejects(update(f.i, async () => "v0.2.0"), /Recovery incomplete/);
  assert.equal(f.pending(), true);
  assert.throws(() => assertReady(f.i.appDir, f.i.home, () => oldCommit), /incomplete/);
  f.i.restore = restore;
  await update(f.i, () => { throw Error("network must not be needed to recover"); });
  assert.deepEqual(f.database(), f.original);
  assert.deepEqual(f.metadataNow(), f.metadata);
  assert.equal(f.pending(), false);
  assert.equal(assertReady(f.i.appDir, f.i.home, () => oldCommit), true);
});

test("same commit still repairs dependencies and validates before declaring success", async t => {
  const f = fixture(t);
  const git = f.i.git;
  f.i.git = (...args) => args[0] === "rev-parse" && args[1] !== "HEAD" ? oldCommit : git(...args);
  await update(f.i, async () => "v0.2.0");
  assert.ok(f.calls.includes("backup") && f.calls.includes("sync-new") && f.calls.includes("health-old"));
});

test("managed startup rejects unvalidated or mismatched code; development clones remain usable", t => {
  const f = fixture(t);
  assert.throws(() => assertReady(f.i.appDir, f.i.home, () => newCommit), /not passed/);
  const metadata = { ...f.metadata }; delete metadata.validatedCommit;
  atomicJson(path.join(f.i.home, "install.json"), metadata);
  assert.throws(() => assertReady(f.i.appDir, f.i.home, () => oldCommit), /not passed/);
  assert.equal(assertReady(path.join(f.i.home, "development"), f.i.home), false);
});

test("alternate database settings are rejected before Docker or migration can touch data", () => {
  const i = new Installation(os.tmpdir(), os.tmpdir(), "python");
  i.env.FOCUS_DATABASE_URL = "postgresql+asyncpg://example/external";
  i.compose = () => assert.fail("Docker must not run");
  assert.throws(() => i.prepareDatabase(), /Custom databases/);
});

test("dependency recovery restores the original bytes without npm, pip or network access", t => {
  const f = fixture(t);
  const i = new Installation(f.i.home, f.i.appDir, "python");
  const venv = path.join(i.home, "runtime/venv");
  const modules = path.join(i.appDir, "desktop/node_modules");
  const directory = path.join(i.home, "snapshot");
  for (const folder of [venv, modules, directory]) fs.mkdirSync(folder, { recursive: true });
  fs.writeFileSync(path.join(venv, "original"), "old Python dependencies");
  fs.writeFileSync(path.join(modules, "original"), "old npm dependencies");
  i.run = () => "fixture==1\n";
  i.snapshotDependencies(directory);
  for (const folder of [venv, modules]) {
    fs.writeFileSync(path.join(folder, "original"), "changed");
    fs.writeFileSync(path.join(folder, "new-package"), "new");
  }
  i.run = () => assert.fail("recovery cannot invoke package installers");
  i.restoreDependencies(directory);
  assert.equal(fs.readFileSync(path.join(venv, "original"), "utf8"), "old Python dependencies");
  assert.equal(fs.readFileSync(path.join(modules, "original"), "utf8"), "old npm dependencies");
  assert.ok(!fs.existsSync(path.join(modules, "new-package")));
});

test("the updater process itself holds exclusion and blocks direct startup before backup", async t => {
  const f = fixture(t);
  await withUpdateLock(f.i.home, async () => {
    assert.throws(() => assertReady(f.i.appDir, f.i.home, () => oldCommit), /incomplete/);
    await assert.rejects(withUpdateLock(f.i.home, () => assert.fail("concurrent updater ran")), /still running/);
  });
  assert.equal(assertReady(f.i.appDir, f.i.home, () => oldCommit), true);
  await assert.rejects(withUpdateLock(f.i.home, () => { throw Error("fixture failure"); }), /fixture failure/);
  assert.equal(assertReady(f.i.appDir, f.i.home, () => oldCommit), true);
});

test("restoring a legacy baseline does not turn it into an accepted stable release", async t => {
  const f = fixture(t, "health-new");
  const legacy = { managed: true, repository, appPath: f.i.appDir, branch: "main" };
  atomicJson(path.join(f.i.home, "install.json"), legacy);
  await assert.rejects(update(f.i, async () => "v0.2.0"), /Previous code and database restored/);
  assert.equal(f.metadataNow().channel, undefined);
  assert.equal(f.metadataNow().recoveredCommit, oldCommit);
  assert.equal(assertReady(f.i.appDir, f.i.home, () => oldCommit), true);
  await update(f.i, async () => "v0.2.0");
  assert.equal(f.metadataNow().branch, undefined);
  assert.equal(f.metadataNow().recoveredCommit, undefined);
  assert.throws(() => assertReady(f.i.appDir, f.i.home, () => oldCommit), /not passed/);
});

test("Windows managed startup treats differently cased paths as the same installation", { skip: process.platform !== "win32" }, t => {
  const f = fixture(t);
  assert.equal(assertReady(f.i.appDir.toUpperCase(), f.i.home, () => oldCommit), true);
});

test("an interrupted lock acquisition becomes recoverable without removing a live lock", async t => {
  const f = fixture(t);
  const directory = path.join(f.i.home, "locks/transaction.lock");
  fs.mkdirSync(directory, { recursive: true });
  await assert.rejects(withUpdateLock(f.i.home, () => assert.fail("owner may be starting")), /acquiring/);
  const old = new Date(Date.now() - 60000);
  fs.utimesSync(directory, old, old);
  let recovered = false;
  await withUpdateLock(f.i.home, () => { recovered = true; });
  assert.equal(recovered, true);
});

test("channel options are strict and legacy main is dev", () => {
  assert.equal(channelOption([]), undefined);
  for (const channel of ["dev", "stable"]) assert.equal(channelOption([`--${channel}`]), channel);
  for (const args of [["--dev", "--stable"], ["--dev", "--dev"], ["--channel"], ["dev"]]) {
    assert.throws(() => channelOption(args), /Usage/);
  }
  assert.equal(installedChannel({ branch: "main" }), "dev");
  assert.equal(installedChannel({ channel: "stable", branch: "main" }), "stable");
  assert.throws(() => installedChannel({ channel: "other", branch: "main" }), /Unknown/);
});

for (const initial of ["dev", "stable"]) for (const selected of [undefined, "dev", "stable"]) {
  test(`${initial} update with ${selected || "default"} persists only the selected channel`, async t => {
    const channel = selected || initial, f = fixture(t, undefined, initial);
    let resolutions = 0;
    await update(f.i, async () => { resolutions++; return "v0.2.0"; }, selected);
    assert.equal(resolutions, channel === "stable" ? 1 : 0);
    assert.deepEqual(f.refs, [channel === "dev" ? "refs/heads/main" : "refs/tags/v0.2.0"]);
    assert.equal(f.metadataNow().channel, channel);
    assert.equal(f.metadataNow().release, channel === "stable" ? "v0.2.0" : undefined);
    assert.equal(assertReady(f.i.appDir, f.i.home, () => newCommit), true);
    assert.ok(f.calls.includes("backup") && f.calls.includes("migrate") && f.calls.includes("health-new"));
  });
}

test("missing stable Release leaves a dev installation untouched; dev never resolves it", async t => {
  const f = fixture(t, undefined, "dev");
  const unavailable = () => { throw Error("No stable release available"); };
  await assert.rejects(update(f.i, unavailable, "stable"), /No stable release/);
  assert.deepEqual(f.metadataNow(), f.metadata);
  assert.deepEqual(f.calls, []);
  await update(f.i, unavailable);
  assert.equal(f.metadataNow().channel, "dev");
});

for (const initial of ["dev", "stable"]) {
  test(`${initial} interrupted switch restores its prior channel, CLI and history offline`, async t => {
    const f = fixture(t, "publish", initial), selected = initial === "dev" ? "stable" : "dev";
    const restore = f.i.restoreCli;
    f.i.restoreCli = () => { throw Error("CLI recovery unavailable"); };
    await assert.rejects(update(f.i, async () => "v0.2.0", selected), /Recovery incomplete/);
    assert.equal(f.pending(), true);
    assert.throws(() => assertReady(f.i.appDir, f.i.home, () => oldCommit), /incomplete/);
    f.i.restoreCli = restore;
    const git = f.i.git;
    f.i.git = (...args) => { assert.notEqual(args[0], "fetch"); return git(...args); };
    await update(f.i, () => assert.fail("recovery cannot resolve Release"), selected);
    assert.equal(f.pending(), false);
    assert.deepEqual(f.metadataNow(), f.metadata);
    assert.deepEqual(f.database(), f.original);
    assert.deepEqual(f.cli(), { generation: "cli-old" });
  });
}

test("legacy main defaults to dev without a Release and removes obsolete metadata after acceptance", async t => {
  const f = fixture(t);
  atomicJson(path.join(f.i.home, "install.json"), { managed: true, repository, appPath: f.i.appDir, branch: "main" });
  await update(f.i, () => assert.fail("legacy main must not query Release"));
  assert.equal(f.metadataNow().channel, "dev");
  assert.equal(f.metadataNow().branch, undefined);
  assert.equal(assertReady(f.i.appDir, f.i.home, () => newCommit), true);
});

test("CLI generations publish as a complete bundle and retain modern controller for old releases", t => {
  const f = fixture(t), i = new Installation(f.i.home, f.i.appDir, "python");
  const { cliDirectory } = require("../../../scripts/cli-launch.cjs");
  const root = path.join(i.home, "runtime/cli");
  const source = path.resolve(__dirname, "../../../scripts");
  i.publishCli(); // target has no protocol: use current modern controller
  const previous = i.snapshotCli();
  const oldDirectory = cliDirectory(root);
  assert.equal(fs.readFileSync(path.join(oldDirectory, "managed-update.cjs"), "utf8"), fs.readFileSync(path.join(source, "managed-update.cjs"), "utf8"));
  const target = path.join(i.appDir, "scripts");
  fs.cpSync(oldDirectory, target, { recursive: true });
  fs.appendFileSync(path.join(target, "managed-update.cjs"), "\n// new controller\n");
  // Simulate interruption mid-copy: the old generation must remain selected.
  const copy = fs.copyFileSync;
  t.mock.method(fs, "copyFileSync", (from, to) => {
    if (path.basename(from) === "focus.sh") throw Error("interrupted copy");
    return copy(from, to);
  });
  assert.throws(() => i.publishCli(), /interrupted copy/);
  assert.equal(cliDirectory(root), oldDirectory);
  t.mock.restoreAll();
  i.publishCli();
  assert.notEqual(cliDirectory(root), oldDirectory);
  assert.match(fs.readFileSync(path.join(cliDirectory(root), "managed-update.cjs"), "utf8"), /new controller/);
  for (const file of ["focus.ps1", "focus.sh", "managed-update.cjs", "cli-launch.cjs", "cli-protocol.json"]) assert.ok(fs.existsSync(path.join(cliDirectory(root), file)));
  i.restoreCli(previous);
  assert.equal(cliDirectory(root), oldDirectory);
  i.restoreCli(null);
  assert.equal(cliDirectory(root), root);
});

test("acceptance metadata failure rolls back a published real CLI pointer", async t => {
  const f = fixture(t), real = new Installation(f.i.home, f.i.appDir, "python");
  real.publishCli();
  const previous = real.snapshotCli();
  for (const method of ["snapshotCli", "restoreCli", "publishCli"]) f.i[method] = real[method].bind(real);
  const rename = fs.renameSync;
  let injected = false;
  t.mock.method(fs, "renameSync", (from, to) => {
    if (!injected && to === path.join(f.i.home, "install.json")) { injected = true; throw Error("metadata unavailable"); }
    return rename(from, to);
  });
  await assert.rejects(update(f.i, async () => "v0.2.0", "dev"), /metadata unavailable/);
  assert.deepEqual(real.snapshotCli(), previous);
  assert.deepEqual(f.metadataNow(), f.metadata);
  assert.deepEqual(f.database(), f.original);
});

test("pre-generation recovery journals still restore offline without a CLI snapshot", async t => {
  const f = fixture(t, "health-new"), restore = f.i.restore;
  f.i.restore = () => { throw Error("restore unavailable"); };
  await assert.rejects(update(f.i, async () => "v0.2.0"), /Recovery incomplete/);
  const file = path.join(f.i.home, "update-state.json");
  const journal = JSON.parse(fs.readFileSync(file));
  delete journal.previousCli;
  atomicJson(file, journal);
  f.i.restore = restore;
  f.i.restoreCli = () => assert.fail("old updater never saved a CLI snapshot");
  await update(f.i, () => assert.fail("recovery queried Releases"));
  assert.deepEqual(f.metadataNow(), f.metadata);
  assert.deepEqual(f.database(), f.original);
  assert.equal(f.pending(), false);
});
