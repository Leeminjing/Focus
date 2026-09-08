const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const root = path.resolve(__dirname, "..");
const read = relative => fs.readFileSync(path.join(root, relative), "utf8");
const installer = read("install.sh");
const macCli = read("scripts/focus.sh");
const windowsInstaller = read("install.ps1");
const windowsCli = read("scripts/focus.ps1");
const gitAttributes = read(".gitattributes");
const main = read("desktop/main.cjs");
const readme = read("README.md");
const readmeZh = read("README.zh-CN.md");

function shellCommand() {
  if (process.platform !== "win32") return "sh";
  const git = spawnSync("git", ["--exec-path"], { encoding: "utf8" });
  if (git.status !== 0) return null;
  const candidate = path.resolve(git.stdout.trim(), "..", "..", "..", "bin", "sh.exe");
  return fs.existsSync(candidate) ? candidate : null;
}

const shell = shellCommand();
assert.ok(shell, "a POSIX shell is required for managed distribution syntax checks");
assert.ok(!installer.includes("\r") && !macCli.includes("\r"), "macOS shell entry points must use LF line endings");
assert.match(gitAttributes, /^\*\.sh text eol=lf$/m);
for (const script of ["install.sh", "scripts/focus.sh"]) {
  const result = spawnSync(shell, ["-n", path.join(root, script)], { encoding: "utf8" });
  assert.equal(result.status, 0, `${script} must pass sh -n: ${result.stderr}`);
}

assert.ok(installer.indexOf('"$(uname -s)" = "Darwin"') < installer.indexOf('mkdir -p "$focus_home"'), "Darwin and prerequisites are checked before installation writes");
for (const prerequisite of ["git", "node", "npm", "docker"]) {
  assert.match(installer, new RegExp(`require_command ${prerequisite}\\b`));
}
assert.match(installer, /Python 3\.11 or newer/);
assert.match(installer, /managed_install_matches \|\| fail/);
assert.match(installer, /sh "\$cli_script" update/);
assert.match(installer, /chmod 755 "\$bin_dir\/focus"/);
assert.match(installer, /export PATH="\$HOME\/\.focus\/bin:\$PATH"/);

assert.match(macCli, /case "\$#:\$\*" in[\s\S]*0:\) start_focus[\s\S]*1:update\) update_focus/);
assert.match(macCli, /git -C "\$app_dir" fetch --prune origin "\$expected_branch"/);
assert.match(macCli, /git -C "\$app_dir" reset --hard "origin\/\$expected_branch"/);
assert.match(macCli, /git -C "\$app_dir" reset --hard "\$previous_commit"/);
assert.match(macCli, /npm --prefix "\$desktop_dir" ci \|\| return \$\?/);
assert.match(macCli, /pip install[\s\S]*\|\| return \$\?/);
assert.match(macCli, /lock_is_active "\$locks_dir\/running\.lock"/);
assert.match(macCli, /kill -0 "\$owner"/);
assert.match(macCli, /FOCUS_GLOBAL_HOME="\$focus_home"/);
assert.match(macCli, /FOCUS_PYTHON="\$venv_python"/);

assert.match(windowsInstaller, /This installer currently supports Windows only/);
assert.match(windowsCli, /reset", "--hard", "origin\/\$ExpectedBranch"/);
assert.match(main, /if \(process\.platform !== "darwin"\) \{[\s\S]*mainWindowOptions\.titleBarOverlay/);
assert.match(main, /process\.platform === "win32"/);

const macInstallCommand = "curl -fsSL https://raw.githubusercontent.com/Leeminjing/Focus/main/install.sh | sh";
assert.ok(readme.includes(macInstallCommand));
assert.ok(readmeZh.includes(macInstallCommand));
for (const document of [readme, readmeZh]) {
  assert.match(document, /focus update/);
  assert.match(document, /~\/\.focus\/app/);
  assert.match(document, /~\/\.focus\/runtime/);
  assert.match(document, /~\/\.focus\/bin/);
}

console.log("managed-distribution: Windows/macOS entry points, update safety, shell syntax, Electron platform options, and docs passed");
