const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const root = path.resolve(__dirname, "../../..");
const read = relative => fs.readFileSync(path.join(root, relative), "utf8");
const installer = read("install.sh");
const macCli = read("scripts/focus.sh");
const windowsInstaller = read("install.ps1");
const windowsCli = read("scripts/focus.ps1");
const gitAttributes = read(".gitattributes");
const main = read("desktop/src/main/main.cjs");
const updater = read("scripts/managed-update.cjs");
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
if (process.platform === "win32") {
  for (const script of ["install.ps1", "scripts/focus.ps1"]) {
    const parser = "$errors = $null; [System.Management.Automation.Language.Parser]::ParseFile($env:FOCUS_PARSE_SCRIPT, [ref]$null, [ref]$errors) | Out-Null; if ($errors) { throw ($errors.Message -join '; ') }";
    const result = spawnSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", parser], {
      windowsHide: true, encoding: "utf8", env: { ...process.env, FOCUS_PARSE_SCRIPT: path.join(root, script) },
    });
    assert.equal(result.status, 0, `${script} must parse in Windows PowerShell: ${result.stderr}`);
  }
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
assert.match(macCli, /node "\$updater" update/);
assert.match(updater, /refs\/tags\/\$\{tag\}/);
assert.match(updater, /i\.git\("reset", "--hard", state\.previousCommit\)/);
assert.match(updater, /i\.backup\(directory\)/);
assert.match(updater, /i\.migrate\(\);\s*i\.health\(\)/);
assert.match(macCli, /lock_is_active "\$locks_dir\/running\.lock"/);
assert.match(macCli, /kill -0 "\$owner"/);
assert.match(macCli, /FOCUS_GLOBAL_HOME="\$focus_home"/);
assert.match(macCli, /FOCUS_PYTHON="\$venv_python"/);

assert.match(windowsInstaller, /This installer currently supports Windows only/);
assert.match(windowsCli, /managed-update\.cjs/);
assert.match(main, /if \(process\.platform !== "darwin"\) \{[\s\S]*mainWindowOptions\.titleBarOverlay/);
assert.match(main, /process\.platform === "win32"/);

const macInstallCommand = "curl -fsSL https://github.com/Leeminjing/Focus/releases/latest/download/install.sh | sh";
assert.ok(readme.includes(macInstallCommand));
assert.ok(readmeZh.includes(macInstallCommand));
for (const document of [readme, readmeZh]) {
  assert.match(document, /focus update/);
  assert.match(document, /~\/\.focus\/app/);
  assert.match(document, /~\/\.focus\/runtime/);
  assert.match(document, /~\/\.focus\/bin/);
}

// electron@43 的依赖链（@electron/get@5 为 ESM-only）声明 engines node >= 22.12.0；
// 低于该版本的 Node 会在 electron 的 postinstall 阶段以 ERR_REQUIRE_ESM 失败，
// 从而留下没有二进制的残缺 node_modules。因此必须在安装/同步依赖前显式校验版本。
for (const [name, script] of [
  ["install.sh", installer],
  ["scripts/focus.sh", macCli],
  ["install.ps1", windowsInstaller],
  ["scripts/focus.ps1", windowsCli],
]) {
  assert.match(script, /22\.12\.0/, `${name} must state the required Node.js version`);
}
assert.match(installer, /require_node_runtime/, "install.sh must gate on the Node.js version");
assert.match(macCli, /require_node_runtime/, "scripts/focus.sh must gate on the Node.js version");
assert.match(windowsInstaller, /Assert-NodeRuntime/, "install.ps1 must gate on the Node.js version");
assert.match(windowsCli, /Assert-NodeRuntime/, "scripts/focus.ps1 must gate on the Node.js version");

// 就绪检查必须同时验证 path.txt 和它指向的可执行文件，
// 而不只是 node_modules/electron/package.json 存在——否则残缺安装会被判定为「就绪」。
assert.match(macCli, /electron_path_file="\$electron_dir\/path\.txt"/, "scripts/focus.sh readiness must check the Electron install marker");
assert.match(macCli, /electron_dir\/dist\/\$electron_relative_path/, "scripts/focus.sh readiness must check the Electron executable");
assert.match(windowsCli, /Join-Path \$electronDir "path\.txt"/, "scripts/focus.ps1 readiness must check the Electron install marker");
assert.match(windowsCli, /Join-Path \$electronDir "dist"/, "scripts/focus.ps1 readiness must check the Electron executable");

// 依赖声明本身必须让 npm 在安装期就拒绝不满足 engines 的 Node。
const desktopPackage = JSON.parse(read("desktop/package.json"));
assert.equal(
  desktopPackage.engines && desktopPackage.engines.node,
  ">=22.12.0",
  "desktop/package.json must declare the required Node.js engine"
);
assert.equal(
  desktopPackage.scripts && desktopPackage.scripts["install:electron"],
  "install-electron",
  "desktop/package.json must expose Electron's binary installer"
);

// electron@43.2.0 不再声明依赖级 postinstall；npm ci 只安装 JS 包，必须由 Focus
// 显式调用 install-electron，否则 path.txt 永远不会生成，focus/update 会形成死循环。
const macNpmCi = '["ci"]';
const macElectronInstall = '["run", "install:electron"]';
assert.ok(updater.includes(macElectronInstall), "scripts/focus.sh must explicitly install the Electron runtime");
assert.ok(
  updater.indexOf(macNpmCi) < updater.indexOf(macElectronInstall),
  "scripts/focus.sh must install Electron after npm ci"
);
assert.ok(
  fs.existsSync(path.join(root, "desktop/.npmrc")),
  "desktop/.npmrc must exist so engine mismatches fail the install"
);
assert.match(read("desktop/.npmrc"), /^engine-strict=true$/m);

// 文档必须与 engines 一致，否则用户在安装前无从得知版本要求。
for (const [name, document] of [["README.md", readme], ["README.zh-CN.md", readmeZh]]) {
  assert.match(document, /22\.12\.0/, `${name} must state the required Node.js version`);
}

console.log("managed-distribution: Windows/macOS entry points, update safety, shell syntax, Electron platform options, Node engine gate, and docs passed");
