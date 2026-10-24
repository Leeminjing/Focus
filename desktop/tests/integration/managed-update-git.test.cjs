/* Real local Git and process interruption; no application or user database. */
"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const cp = require("node:child_process");
const { atomicJson } = require("../../../scripts/managed-update.cjs");
const { cliDirectory } = require("../../../scripts/cli-launch.cjs");
const source = path.resolve(__dirname, "../../../scripts");
const repository = "https://github.com/Leeminjing/Focus.git";

test("real Git switches and abrupt termination preserve code, external CLI, dependencies and history", t => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "focus-git-update-"));
  t.after(() => fs.rmSync(home, { recursive: true, force: true }));
  const remote = path.join(home, "remote"), app = path.join(home, "app");
  fs.mkdirSync(remote);
  const git = (cwd, ...args) => cp.execFileSync("git", ["-C", cwd, ...args], { encoding: "utf8", stdio: "pipe", windowsHide: true }).trim();
  git(remote, "init", "-b", "main");
  git(remote, "config", "user.email", "fixture@example.invalid");
  git(remote, "config", "user.name", "Update fixture");
  const copyScripts = () => {
    fs.mkdirSync(path.join(remote, "scripts"), { recursive: true });
    for (const name of ["focus.ps1", "focus.sh", "managed-update.cjs", "cli-launch.cjs", "cli-protocol.json"]) {
      fs.copyFileSync(path.join(source, name), path.join(remote, "scripts", name));
    }
  };
  copyScripts();
  fs.writeFileSync(path.join(remote, "code"), "baseline");
  git(remote, "add", "."); git(remote, "commit", "-m", "baseline");
  const baseline = git(remote, "rev-parse", "HEAD");
  fs.unlinkSync(path.join(remote, "scripts/cli-protocol.json"));
  fs.writeFileSync(path.join(remote, "scripts/managed-update.cjs"), "throw Error('old stable-only updater');\n");
  fs.writeFileSync(path.join(remote, "code"), "stable");
  git(remote, "add", "."); git(remote, "commit", "-m", "older stable without CLI protocol");
  git(remote, "tag", "v0.2.0");
  const stable = git(remote, "rev-parse", "HEAD");
  copyScripts();
  fs.appendFileSync(path.join(remote, "scripts/managed-update.cjs"), "\n// latest main controller\n");
  fs.writeFileSync(path.join(remote, "code"), "development");
  git(remote, "add", "."); git(remote, "commit", "-m", "latest main");
  const main = git(remote, "rev-parse", "HEAD");
  cp.execFileSync("git", ["clone", "--single-branch", "--branch", "main", remote, app], { stdio: "pipe", windowsHide: true });
  git(app, "reset", "--hard", baseline);
  atomicJson(path.join(home, "install.json"), { managed: true, repository, appPath: app, branch: "main" });
  const history = { context: "work", revision: "immutable", runs: ["completed", "running"] };
  atomicJson(path.join(home, "data.json"), history);
  fs.writeFileSync(path.join(home, "config.yaml"), "user config\n");
  for (const directory of ["runtime/venv", "app/desktop/node_modules"]) {
    fs.mkdirSync(path.join(home, directory), { recursive: true });
    fs.writeFileSync(path.join(home, directory, "version"), "baseline deps");
  }
  const driver = path.join(home, "driver.cjs");
  fs.writeFileSync(driver, `
    const fs = require('node:fs'), path = require('node:path');
    const home = __dirname, mode = process.argv[2], channel = process.argv[3] || undefined;
    const cliRoot = path.join(home,'runtime/cli');
    const {cliDirectory} = require(${JSON.stringify(path.join(source, "cli-launch.cjs"))});
    const updater = fs.existsSync(path.join(cliRoot,'current.json')) ? path.join(cliDirectory(cliRoot),'managed-update.cjs') : ${JSON.stringify(path.join(source, "managed-update.cjs"))};
    const { Installation, update, withUpdateLock } = require(updater);
    const i = new Installation(home, path.join(home,'app'), 'fixture-python');
    const git = i.git.bind(i), run = i.run.bind(i);
    i.git = (...args) => {
      if (args[0] === 'remote') return ${JSON.stringify(repository)};
      if (mode === 'recover' && args[0] === 'fetch') throw Error('recovery used network');
      return git(...args);
    };
    i.run = (cmd,args,opts) => cmd === 'fixture-python' ? 'fixture==1\\n' : run(cmd,args,opts);
    i.assertStopped = i.prepareDatabase = i.ensurePython = () => {};
    i.backup = dir => { fs.copyFileSync(path.join(home,'data.json'),path.join(dir,'database.dump')); return 'fixture'; };
    i.restore = dir => fs.copyFileSync(path.join(dir,'database.dump'),path.join(home,'data.json'));
    i.sync = () => {
      for (const dir of ['runtime/venv','app/desktop/node_modules']) fs.writeFileSync(path.join(home,dir,'version'), 'target deps');
    };
    i.migrate = () => {
      fs.writeFileSync(path.join(home,'data.json'), JSON.stringify({partialMigration:true}));
      if (mode === 'interrupt') process.exit(93);
    };
    i.health = () => { if (mode === 'fail' && git('rev-parse','HEAD') === ${JSON.stringify(main)}) throw Error('injected health failure'); };
    const publish = i.publishCli.bind(i);
    i.publishCli = () => { publish(); if (mode === 'publish-interrupt') process.exit(94); };
    withUpdateLock(home, () => update(i, () => {
      if (mode === 'recover') throw Error('recovery resolved a release');
      return 'v0.2.0';
    }, channel)).catch(e => { console.error(e.message); process.exitCode=1; });
  `);
  const invoke = (mode, channel) => cp.spawnSync(process.execPath, [driver, mode, ...(channel ? [channel] : [])], { encoding: "utf8", windowsHide: true });
  const metadata = () => JSON.parse(fs.readFileSync(path.join(home, "install.json")));
  let result = invoke("success");
  assert.equal(result.status, 0, result.stderr);
  assert.equal(git(app, "rev-parse", "HEAD"), main);
  assert.equal(metadata().channel, "dev");
  const cliRoot = path.join(home, "runtime/cli");
  assert.match(fs.readFileSync(path.join(cliDirectory(cliRoot), "managed-update.cjs"), "utf8"), /latest main controller/);
  result = invoke("success", "stable");
  assert.equal(result.status, 0, result.stderr);
  assert.equal(git(app, "rev-parse", "HEAD"), stable);
  assert.equal(metadata().channel, "stable");
  assert.match(fs.readFileSync(path.join(cliDirectory(cliRoot), "managed-update.cjs"), "utf8"), /installedChannel/);
  atomicJson(path.join(home, "data.json"), history);
  const accepted = metadata(), cli = cliDirectory(cliRoot);
  fs.writeFileSync(path.join(app, "code"), "local user configuration edits");
  result = invoke("success", "dev");
  assert.equal(result.status, 1);
  assert.match(result.stderr, /local changes/);
  assert.equal(fs.readFileSync(path.join(app, "code"), "utf8"), "local user configuration edits");
  assert.deepEqual(metadata(), accepted);
  git(app, "restore", "--", "code");
  for (const mode of ["fail", "interrupt", "publish-interrupt"]) {
    result = invoke(mode, "dev");
    assert.equal(result.status, mode === "interrupt" ? 93 : mode === "publish-interrupt" ? 94 : 1, result.stderr);
    if (mode !== "fail") {
      assert.ok(fs.existsSync(path.join(home, "update-state.json")));
      result = invoke("recover", "dev");
      assert.equal(result.status, 0, result.stderr);
    }
    assert.equal(git(app, "rev-parse", "HEAD"), stable);
    assert.deepEqual(metadata(), accepted);
    assert.equal(cliDirectory(cliRoot), cli);
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(home, "data.json"))), history);
    assert.equal(fs.readFileSync(path.join(home, "runtime/venv/version"), "utf8"), "target deps");
    assert.equal(fs.readFileSync(path.join(home, "config.yaml"), "utf8"), "user config\n");
    assert.ok(!fs.existsSync(path.join(home, "update-state.json")));
  }
});
