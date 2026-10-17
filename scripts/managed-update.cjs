/* Managed release transaction. Kept outside the checkout during recovery so a
 * failed release cannot replace the code needed to restore its predecessor. */
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const cp = require("node:child_process");

const repository = "https://github.com/Leeminjing/Focus.git";
const releaseApi = "https://api.github.com/repos/Leeminjing/Focus/releases/latest";
const normalize = value => value.trim().toLowerCase().replace(/\/$/, "").replace(/\.git$/, "");
const canonicalPath = value => process.platform === "win32" ? path.resolve(value).toLowerCase() : path.resolve(value);

function hashFile(file) {
  const hash = crypto.createHash("sha256");
  const fd = fs.openSync(file, "r");
  const buffer = Buffer.alloc(1024 * 1024);
  try {
    for (let count; (count = fs.readSync(fd, buffer)) > 0;) hash.update(buffer.subarray(0, count));
  } finally { fs.closeSync(fd); }
  return hash.digest("hex");
}

function atomicJson(file, value) {
  const temporary = `${file}.tmp`;
  const fd = fs.openSync(temporary, "w", 0o600);
  try {
    fs.writeFileSync(fd, JSON.stringify(value, null, 2) + "\n");
    fs.fsyncSync(fd);
  } finally { fs.closeSync(fd); }
  fs.renameSync(temporary, file);
  if (process.platform !== "win32") {
    const parent = fs.openSync(path.dirname(file), "r");
    try { fs.fsyncSync(parent); } finally { fs.closeSync(parent); }
  }
}

async function withUpdateLock(home, operation) {
  const directory = path.join(home, "locks", "transaction.lock");
  fs.mkdirSync(path.dirname(directory), { recursive: true });
  if (fs.existsSync(directory)) {
    let owner;
    try { owner = JSON.parse(fs.readFileSync(path.join(directory, "owner.json"), "utf8")).pid; } catch {}
    if (Number.isSafeInteger(owner) && owner > 0) {
      try { process.kill(owner, 0); throw new Error("Another Focus update is still running."); }
      catch (error) { if (error.code !== "ESRCH") throw error; }
    } else if (Date.now() - fs.statSync(directory).mtimeMs < 30000) {
      throw new Error("Another Focus update is acquiring its lock. Retry shortly.");
    }
    fs.rmSync(directory, { recursive: true });
  }
  // mkdir remains atomic if two contenders both observed an absent/stale lock.
  fs.mkdirSync(directory);
  try {
    atomicJson(path.join(directory, "owner.json"), { pid: process.pid });
    return await operation();
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
}

async function stableRelease(fetcher = fetch) {
  const response = await fetcher(releaseApi, {
    headers: { Accept: "application/vnd.github+json", "User-Agent": "Focus-installer" },
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw new Error(`No stable release available (GitHub HTTP ${response.status}).`);
  const release = await response.json();
  if (release.draft !== false || release.prerelease !== false || !release.published_at
      || !/^v\d+\.\d+\.\d+$/.test(release.tag_name)) {
    throw new Error("Expected a published stable vX.Y.Z release; refusing to follow main.");
  }
  return release.tag_name;
}

function assertReady(appDir, home, execute = cp.execFileSync) {
  const metadataPath = path.join(home, "install.json");
  if (!fs.existsSync(metadataPath)) {
    if (canonicalPath(appDir) === canonicalPath(path.join(home, "app"))) throw new Error("Managed install.json is missing. Run the latest Focus installer.");
    return false;
  }
  const metadata = JSON.parse(fs.readFileSync(metadataPath, "utf8").replace(/^\uFEFF/, ""));
  if (!metadata.managed || !metadata.appPath || canonicalPath(metadata.appPath) !== canonicalPath(appDir)) {
    if (canonicalPath(appDir) === canonicalPath(path.join(home, "app"))) throw new Error("Managed installation metadata is invalid. Run the latest Focus installer.");
    return false;
  }
  if (fs.existsSync(path.join(home, "update-state.json")) || fs.existsSync(path.join(home, "locks/transaction.lock"))) {
    throw new Error("Focus update/recovery is incomplete. Close Focus and run 'focus update' to restore the recovery point.");
  }
  const head = execute("git", ["-C", appDir, "rev-parse", "HEAD"], { encoding: "utf8", windowsHide: true }).trim();
  if (metadata.channel !== "stable" || !metadata.validatedCommit || metadata.validatedCommit !== head) {
    if (!metadata.validatedCommit && metadata.channel !== "stable" && metadata.branch === "main" && metadata.recoveredCommit === head) return true;
    throw new Error("This managed version has not passed update validation. Run the latest Focus installer.");
  }
  return true;
}

class Installation {
  constructor(home, appDir, python) {
    this.home = path.resolve(home);
    this.appDir = path.resolve(appDir);
    this.python = python;
    this.env = { ...process.env, FOCUS_GLOBAL_HOME: this.home, FOCUS_MANAGED_APP: this.appDir,
      PYTHONUTF8: "1", PYTHONPATH: [path.join(this.appDir, "backend/packages/harness"), this.appDir].join(path.delimiter) };
    this.composeFile = path.join(this.appDir, "desktop/compose.yaml");
  }
  run(command, args, options = {}) {
    return cp.execFileSync(command, args, { cwd: this.appDir, env: this.env,
      encoding: "utf8", windowsHide: true, stdio: "inherit", ...options });
  }
  git(...args) { return this.run("git", ["-C", this.appDir, ...args], { stdio: "pipe" }).trim(); }
  compose(args, options) { return this.run("docker", ["compose", "-f", this.composeFile, ...args], options); }
  ensurePython() {
    if (fs.existsSync(this.python)) return;
    const candidates = process.platform === "win32" ? [["py", "-3"], ["python"]] : [["python3"], ["python"]];
    for (const [command, ...prefix] of candidates) {
      try {
        this.run(command, [...prefix, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"], { stdio: "pipe", timeout: 10000 });
      } catch { continue; }
      this.run(command, [...prefix, "-m", "venv", path.join(this.home, "runtime/venv")]);
      return;
    }
    throw new Error("Python 3.11 or newer is required.");
  }
  prepareDatabase(recovering = false) {
    // The first public distribution manages the bundled local PostgreSQL only.
    // Reject alternate databases before making any changes, rather than back up
    // one database and accidentally migrate another.
    const expected = `postgresql+asyncpg://focus:qweasdzxc123@127.0.0.1:7221/focus`;
    if ((this.env.FOCUS_DATABASE_URL && this.env.FOCUS_DATABASE_URL !== expected)
        || ["FOCUS_DB_USER", "FOCUS_DB_PASSWORD", "FOCUS_DB_NAME", "FOCUS_DB_PORT"].some(key => this.env[key])) {
      throw new Error("Managed updates require the bundled default PostgreSQL. Custom databases need a separate backup/restore procedure.");
    }
    this.env.FOCUS_DATABASE_URL = expected;
    this.compose(["up", "-d", "--pull", recovering ? "never" : "missing", "--wait"]);
  }
  assertStopped() {
    if (process.platform === "win32") {
      const script = "$ErrorActionPreference = 'Stop'; $expected = Join-Path $env:FOCUS_MANAGED_APP 'desktop\\node_modules\\electron\\dist\\electron.exe'; "
        + "Get-CimInstance Win32_Process -Filter \"Name = 'electron.exe'\" | Where-Object { $_.ExecutablePath -ieq $expected } | Select-Object -ExpandProperty ProcessId";
      if (this.run("powershell.exe", ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], { stdio: "pipe" }).trim()) {
        throw new Error("Focus is running. Close it before running 'focus update'.");
      }
      return;
    }
    if (process.platform !== "darwin") return;
    const executable = path.join(this.appDir, "desktop/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron");
    const processes = this.run("ps", ["-axo", "pid=,comm="], { stdio: "pipe" });
    if (processes.split("\n").some(line => line.trim().replace(/^\d+\s+/, "") === executable)) {
      throw new Error("Focus is running. Close it before running 'focus update'.");
    }
  }
  backup(directory) {
    const file = path.join(directory, "database.dump");
    const fd = fs.openSync(file, "wx", 0o600);
    try {
      this.compose(["exec", "-T", "postgres", "pg_dump", "-U", "focus", "-d", "focus", "--format=custom"],
        { encoding: null, stdio: ["ignore", fd, "inherit"] });
      fs.fsyncSync(fd);
    } finally { fs.closeSync(fd); }
    if (!fs.statSync(file).size) throw new Error("Database backup is empty.");
    const input = fs.openSync(file, "r");
    try {
      this.compose(["exec", "-T", "postgres", "pg_restore", "--list"], { stdio: [input, "ignore", "inherit"] });
    } finally { fs.closeSync(input); }
    return hashFile(file);
  }
  restore(directory, state) {
    const file = path.join(directory, "database.dump");
    const checksum = hashFile(file);
    if (checksum !== state.backupSha256) throw new Error("Recovery archive checksum mismatch.");
    // Drop/recreate the database: --clean alone leaves tables added by a failed
    // migration behind. Terminate only connections to the managed Focus DB.
    this.compose(["exec", "-T", "postgres", "psql", "-U", "focus", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c",
      "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'focus' AND pid <> pg_backend_pid()"]);
    const input = fs.openSync(file, "r");
    try {
      this.compose(["exec", "-T", "postgres", "pg_restore", "-U", "focus", "-d", "postgres",
        "--clean", "--if-exists", "--create", "--exit-on-error"], { stdio: [input, "inherit", "inherit"] });
    } finally { fs.closeSync(input); }
  }
  snapshotDependencies(directory) {
    fs.writeFileSync(path.join(directory, "requirements.txt"),
      this.run(this.python, ["-m", "pip", "freeze", "--all"], { stdio: "pipe" }), { mode: 0o600 });
    const inventory = {};
    for (const [name, source] of Object.entries({
      venv: path.join(this.home, "runtime/venv"),
      node_modules: path.join(this.appDir, "desktop/node_modules"),
    })) {
      inventory[name] = fs.existsSync(source);
      if (inventory[name]) fs.cpSync(source, path.join(directory, name), { recursive: true, verbatimSymlinks: true });
    }
    atomicJson(path.join(directory, "dependencies.json"), inventory);
  }
  restoreDependencies(directory) {
    const inventory = JSON.parse(fs.readFileSync(path.join(directory, "dependencies.json"), "utf8"));
    for (const [name, destination] of Object.entries({
      venv: path.join(this.home, "runtime/venv"),
      node_modules: path.join(this.appDir, "desktop/node_modules"),
    })) {
      // Only these two fixed managed directories may be replaced. Refuse a
      // junction/symlink at the destination rather than follow it outside home.
      if (fs.existsSync(destination) && fs.lstatSync(destination).isSymbolicLink()) throw new Error(`Refusing to replace linked dependency directory: ${destination}`);
      const parent = fs.realpathSync(path.dirname(destination));
      if (!parent.startsWith(fs.realpathSync(this.home) + path.sep)) throw new Error("Dependency destination is outside the managed installation.");
      if (inventory[name] && !fs.existsSync(path.join(directory, name))) throw new Error(`Missing ${name} recovery snapshot.`);
      fs.rmSync(destination, { recursive: true, force: true });
      if (inventory[name]) fs.cpSync(path.join(directory, name), destination, { recursive: true, verbatimSymlinks: true });
    }
  }
  sync() {
    // npm.cmd needs cmd.exe on Windows; no user input is interpolated into it.
    if (process.platform === "win32") {
      for (const args of [["ci"], ["run", "install:electron"]]) {
        this.run(process.env.ComSpec || "cmd.exe", ["/d", "/s", "/c", `npm ${args.join(" ")}`], { cwd: path.join(this.appDir, "desktop") });
      }
    } else {
      for (const args of [["ci"], ["run", "install:electron"]]) this.run("npm", args, { cwd: path.join(this.appDir, "desktop") });
    }
    const args = ["-r", path.join(this.appDir, "scripts/desktop-requirements.txt")];
    this.run(this.python, ["-m", "pip", "install", "--disable-pip-version-check", ...args]);
    this.run(this.python, ["-m", "pip", "install", "--no-deps", "-e", path.join(this.appDir, "backend/packages/harness")]);
    this.run(this.python, ["-c", "import alembic, asyncpg, fastapi, psycopg, uvicorn"]);
    const electron = path.join(this.appDir, "desktop/node_modules/electron");
    const executable = fs.readFileSync(path.join(electron, "path.txt"), "utf8").trim();
    if (!executable || !fs.existsSync(path.join(electron, "dist", executable))) throw new Error("Electron executable is missing.");
    this.run(process.execPath, [path.join(this.appDir, "desktop/startup-runtime.cjs"), "--prepare", this.python, this.home],
      { env: { ...this.env, PYTHONPATH: process.env.PYTHONPATH } });
  }
  migrate() {
    this.run(this.python, ["-m", "alembic", "-c", path.join(this.appDir,
      "backend/packages/harness/focus/persistence/migrations/alembic.ini"), "upgrade", "head"]);
  }
  health() {
    this.run(this.python, ["-m", "backend.app.update_check"], { timeout: 120000 });
  }
  publishCli() {
    const directory = path.join(this.home, "runtime/cli");
    fs.mkdirSync(directory, { recursive: true });
    for (const name of ["focus.ps1", "focus.sh", "managed-update.cjs"]) {
      const target = path.join(directory, name);
      fs.copyFileSync(path.join(this.appDir, "scripts", name), `${target}.tmp`);
      fs.renameSync(`${target}.tmp`, target);
    }
  }
}

async function update(installation, resolveRelease = stableRelease) {
  const i = installation;
  const statePath = path.join(i.home, "update-state.json");
  const metadataPath = path.join(i.home, "install.json");
  const metadata = JSON.parse(fs.readFileSync(metadataPath, "utf8").replace(/^\uFEFF/, ""));
  if (!metadata.managed || normalize(metadata.repository) !== normalize(repository)
      || canonicalPath(metadata.appPath) !== canonicalPath(i.appDir) || normalize(i.git("remote", "get-url", "origin")) !== normalize(repository)) {
    throw new Error("Refusing to update an unmanaged checkout or unexpected repository.");
  }
  i.assertStopped();
  // Retrying an interrupted transaction always restores the old pair first.
  // Its journal is removed only after both restores have succeeded.
  async function recover(state) {
    const directory = path.join(i.home, "backups", state.id);
    if (!/^update-[a-zA-Z0-9-]+$/.test(state.id) || !/^[a-f0-9]{40}$/.test(state.previousCommit)) throw new Error("Invalid recovery journal.");
    i.git("reset", "--hard", state.previousCommit);
    i.prepareDatabase(true);
    i.restore(directory, state);
    i.restoreDependencies(directory);
    if (state.previousMetadata.validatedCommit) i.health();
    const previousMetadata = { ...state.previousMetadata };
    if (previousMetadata.branch === "main" && !previousMetadata.validatedCommit) previousMetadata.recoveredCommit = state.previousCommit;
    atomicJson(metadataPath, previousMetadata);
    fs.unlinkSync(statePath);
    console.log(`Previous code and database restored. Recovery point retained: ${directory}`);
  }
  if (fs.existsSync(statePath)) {
    await recover(JSON.parse(fs.readFileSync(statePath, "utf8")));
    console.log("Interrupted update recovered. Run 'focus update' again to try the stable release.");
    return;
  }
  const tag = await resolveRelease();
  i.git("fetch", "--no-tags", "origin", `refs/tags/${tag}`);
  const target = i.git("rev-parse", "FETCH_HEAD^{commit}");
  const previous = i.git("rev-parse", "HEAD");
  i.prepareDatabase();
  i.ensurePython();
  const id = `update-${Date.now()}-${crypto.randomBytes(4).toString("hex")}`;
  const directory = path.join(i.home, "backups", id);
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  i.snapshotDependencies(directory);
  const state = { id, previousCommit: previous, targetCommit: target, tag,
    previousMetadata: metadata, backupSha256: i.backup(directory) };
  // Save a standalone recovery entry point before changing the checkout.
  fs.copyFileSync(__filename, path.join(directory, "recover.cjs"));
  atomicJson(path.join(directory, "recovery.json"), state);
  atomicJson(statePath, state);
  console.log(`Recovery point: ${directory}`);
  try {
    i.git("reset", "--hard", target);
    i.sync();
    i.migrate();
    i.health();
    i.publishCli();
    const accepted = { ...metadata, schema: 2, channel: "stable", release: tag,
      validatedCommit: target, validatedAt: new Date().toISOString(), recoveryPoint: directory };
    delete accepted.branch;
    delete accepted.recoveredCommit;
    atomicJson(metadataPath, accepted);
    fs.unlinkSync(statePath);
  } catch (failure) {
    try { await recover(state); }
    catch (recoveryFailure) {
      throw new Error(`Focus update failed: ${failure.message}. Recovery incomplete: ${recoveryFailure.message}. `
        + `Do not start Focus. Retry 'focus update'. Recovery point: ${directory}`);
    }
    throw new Error(`Focus update failed: ${failure.message}. Previous code and database restored.`);
  }
  console.log(`Focus updated successfully: ${tag} (${target.slice(0, 7)}); migrations and health checks passed.`);
}

module.exports = { Installation, update, stableRelease, assertReady, atomicJson, withUpdateLock };
if (require.main === module) {
  const [command, home, appDir, python] = process.argv.slice(2);
  if (command === "check" && home && appDir) {
    assertReady(appDir, home);
  } else {
    if (command !== "update" || !home || !appDir || !python) throw new Error("Usage: node managed-update.cjs update HOME APP PYTHON");
    withUpdateLock(home, () => update(new Installation(home, appDir, python)))
      .catch(error => { console.error(error.message); process.exitCode = 1; });
  }
}
