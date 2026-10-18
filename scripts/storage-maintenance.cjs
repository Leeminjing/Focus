/* Local backup inventory and retention. Mutations share the updater's lock.
 * Only complete, recognised direct directories of HOME/backups may be removed.
 * Dependency snapshots may contain links; traversal never follows them. */
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { Installation, atomicJson, withUpdateLock } = require("./managed-update.cjs");

function readJson(file, fallback) {
  try { return JSON.parse(fs.readFileSync(file, "utf8").replace(/^\uFEFF/, "")); }
  catch (error) { if (error.code === "ENOENT") return fallback; throw error; }
}

function measure(file, allowDependencyLinks = false) {
  const stat = fs.lstatSync(file);
  if (stat.isSymbolicLink()) return { bytes: 0, linked: true };
  if (!stat.isDirectory()) return { bytes: stat.size, linked: false };
  let bytes = 0, linked = false;
  for (const name of fs.readdirSync(file)) {
    const childPath = path.join(file, name);
    const child = measure(childPath);
    // Only real dependency snapshot directories may contain links. The backup
    // root, snapshot roots, manifests and database archive must stay unlinked.
    const dependency = allowDependencyLinks && ["venv", "node_modules"].includes(name)
      && fs.lstatSync(childPath).isDirectory();
    bytes += child.bytes; linked ||= child.linked && !dependency;
  }
  return { bytes, linked };
}

function inventory(home, policy, now = Date.now()) {
  if (!Number.isSafeInteger(policy?.backup_keep_count) || policy.backup_keep_count < 1 || policy.backup_keep_count > 100
      || !Number.isSafeInteger(policy.backup_retention_days) || policy.backup_retention_days < 1 || policy.backup_retention_days > 3650) {
    throw new Error("Invalid backup retention policy; refusing cleanup.");
  }
  const root = path.resolve(home, "backups");
  const state = readJson(path.join(home, "update-state.json"), null);
  const install = readJson(path.join(home, "install.json"), {});
  if (!fs.existsSync(root)) return { bytes: 0, entries: [], pending_recovery: Boolean(state) };
  if (fs.lstatSync(root).isSymbolicLink()) throw new Error("Backup directory must not be a link.");
  const entries = fs.readdirSync(root).map(id => {
    const directory = path.join(root, id);
    const size = measure(directory, /^update-\d+-[a-f0-9]+$/.test(id));
    const row = { id, bytes: size.bytes, kind: "unknown", created_at: null, protected_reason: "unrecognised_or_incomplete" };
    if (size.linked || !fs.lstatSync(directory).isDirectory()) return { ...row, protected_reason: "linked_or_not_directory" };
    let manifest;
    try {
      if (/^update-\d+-[a-f0-9]+$/.test(id)) {
        manifest = readJson(path.join(directory, "recovery.json"), null);
        if (manifest?.id === id && /^[a-f0-9]{64}$/.test(manifest.backupSha256)
            && ["database.dump", "dependencies.json", "recover.cjs"].every(name => fs.existsSync(path.join(directory, name)))) {
          row.kind = "update"; row.created_at = new Date(Number(id.split("-")[1])).toISOString();
        }
      } else if (/^regular-\d+-[a-f0-9]+$/.test(id)) {
        manifest = readJson(path.join(directory, "backup.json"), null);
        if (manifest?.id === id && manifest.schema === 1 && /^[a-f0-9]{64}$/.test(manifest.sha256)
            && fs.existsSync(path.join(directory, "database.dump")) && Number.isFinite(Date.parse(manifest.created_at))) {
          row.kind = "regular"; row.created_at = manifest.created_at;
        }
      }
    } catch { return row; }
    if (row.kind === "unknown") return row;
    row.protected_reason = null;
    if (state) row.protected_reason = state.id === id ? "active_recovery" : "recovery_pending";
    if (install.recoveryPoint && path.resolve(install.recoveryPoint).toLowerCase() === directory.toLowerCase()) row.protected_reason = "accepted_recovery_point";
    return row;
  });
  for (const kind of ["update", "regular"]) {
    const group = entries.filter(row => row.kind === kind).sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at) || b.id.localeCompare(a.id));
    group.forEach((row, index) => {
      if (index < policy.backup_keep_count) row.protected_reason ||= "recent_backup";
      if (now - Date.parse(row.created_at) < policy.backup_retention_days * 86400000) row.protected_reason ||= "within_retention";
    });
  }
  return { bytes: entries.reduce((sum, row) => sum + row.bytes, 0), entries, pending_recovery: Boolean(state) };
}

function cleanup(home, policy, ids) {
  const current = inventory(home, policy);
  const selected = new Set(ids);
  const removed = [];
  const errors = [];
  for (const row of current.entries) {
    if (!selected.has(row.id) || row.protected_reason) continue;
    try {
      const backupRoot = path.resolve(home, "backups");
      if (fs.lstatSync(backupRoot).isSymbolicLink()) throw new Error("Backup directory must not be a link.");
      const root = fs.realpathSync(backupRoot);
      const target = path.resolve(root, row.id);
      if (path.dirname(target) !== root || fs.lstatSync(target).isSymbolicLink()
          || fs.realpathSync(target) !== target || measure(target, row.kind === "update").linked) throw new Error("Unsafe backup cleanup target.");
      // rm removes nested symbolic links/junctions themselves, not their targets.
      fs.rmSync(target, { recursive: true });
      removed.push(row.id);
    } catch (error) { errors.push({ id: row.id, message: error.message }); }
  }
  return { removed, errors };
}

function backup(installation, now = Date.now()) {
  const i = installation;
  if (fs.existsSync(path.join(i.home, "update-state.json"))) throw new Error("Finish Focus recovery before creating another backup.");
  const root = path.join(i.home, "backups");
  fs.mkdirSync(root, { recursive: true, mode: 0o700 });
  if (fs.lstatSync(root).isSymbolicLink()) throw new Error("Backup directory must not be a link.");
  i.prepareDatabase();
  const id = `regular-${now}-${crypto.randomBytes(4).toString("hex")}`;
  const directory = path.join(root, id);
  fs.mkdirSync(directory, { mode: 0o700 });
  // No manifest is published until pg_dump, pg_restore --list and SHA-256 succeed.
  // An interrupted/failed directory remains visible and protected for inspection.
  const sha256 = i.backup(directory);
  const manifest = { schema: 1, id, created_at: new Date(now).toISOString(), sha256,
    scope: "postgresql", commit: i.git("rev-parse", "HEAD") };
  atomicJson(path.join(directory, "backup.json"), manifest);
  return manifest;
}

module.exports = { inventory, cleanup, backup, measure };
if (require.main === module) {
  const [command, home, appDir, python] = process.argv.slice(2);
  const payload = JSON.parse(fs.readFileSync(0, "utf8"));
  const execute = () => {
    if (command === "inventory") return inventory(home, payload.policy);
    if (command === "cleanup") return cleanup(home, payload.policy, payload.ids);
    if (command === "backup") {
      const installation = new Installation(home, appDir, python);
      const run = installation.run.bind(installation);
      installation.run = (command, args, options = {}) => run(command, args, { stdio: "pipe", ...options });
      return backup(installation);
    }
    throw new Error("Unknown storage maintenance command.");
  };
  // Docker/updater progress goes to stderr so stdout is one JSON response.
  console.log = (...args) => console.error(...args);
  Promise.resolve(command === "inventory" ? execute() : withUpdateLock(home, execute))
    .then(result => process.stdout.write(JSON.stringify(result)))
    .catch(error => { console.error(error.message); process.exitCode = 1; });
}
