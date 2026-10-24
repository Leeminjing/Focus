/* Execute installer logic in an isolated home with native operations replaced. */
"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const cp = require("node:child_process");
const root = path.resolve(__dirname, "../../..");

const harness = String.raw`
$ErrorActionPreference = 'Stop'
$source = Get-Content -Raw -LiteralPath (Join-Path $env:FOCUS_FIXTURE_ROOT 'install.ps1')
$source = $source.Replace('$UserProfile = [Environment]::GetFolderPath("UserProfile")', '$UserProfile = $env:FOCUS_FIXTURE_HOME')
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($source, [ref]$null, [ref]$errors)
if ($errors) { throw ($errors.Message -join '; ') }
$replacements = @{
  'Get-ApplicationPath' = 'param($Name) return $Name'
  'Assert-NodeRuntime' = 'return $env:FOCUS_FIXTURE_NODE'
  'Add-UserPathEntry' = 'param($Path)'
  'Invoke-Native' = @'
param($FilePath, $ArgumentList)
if ($ArgumentList[0] -eq 'clone') {
    $destination = $ArgumentList[-1]
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    Copy-Item -LiteralPath (Join-Path $env:FOCUS_FIXTURE_ROOT 'scripts') -Destination (Join-Path $destination 'scripts') -Recurse
    Copy-Item -LiteralPath (Join-Path $env:FOCUS_FIXTURE_ROOT '.env.example') -Destination (Join-Path $destination '.env.example')
    New-Item -ItemType Directory -Path (Join-Path $destination '.git') | Out-Null
    if ($env:FOCUS_FIXTURE_CASE -eq 'old-release' -and $ArgumentList[2] -ne 'main') {
        $marker = [IO.Path]::GetFullPath((Join-Path $destination 'scripts/cli-protocol.json'))
        if (-not $marker.StartsWith([IO.Path]::GetFullPath($env:FOCUS_FIXTURE_HOME))) { throw 'Unsafe fixture marker' }
        Remove-Item -LiteralPath $marker
    }
    Set-Content -LiteralPath (Join-Path $destination 'code') 'staged'
    return
}
if ($ArgumentList[-2] -ne 'update' -or $ArgumentList[-1] -ne "--$env:FOCUS_FIXTURE_SELECTED") { throw 'Channel was not forwarded' }
$homePath = Join-Path $env:FOCUS_FIXTURE_HOME '.focus'
$appPath = Join-Path $homePath 'app'
$oldCli = Join-Path $homePath 'runtime/cli/managed-update.cjs'
if (Test-Path -LiteralPath $oldCli) {
    if ((Get-Content -Raw -LiteralPath $oldCli).Trim() -ne 'old updater') { throw 'CLI overwritten before transaction' }
    if ((Get-Content -Raw -LiteralPath (Join-Path $appPath 'code')).Trim() -ne 'previous code') { throw 'App replaced before transaction' }
    $state = Get-Content -Raw -LiteralPath (Join-Path $homePath 'install.json') | ConvertFrom-Json
    if ($env:FOCUS_FIXTURE_CASE -eq 'legacy' -and $state.branch -ne 'main') { throw 'Legacy metadata prematurely replaced' }
}
if ($env:FOCUS_FIXTURE_CASE -eq 'failed-upgrade') { throw 'fixture transaction failure' }
$scriptPath = $ArgumentList[-3]
$publisher = "const {Installation}=require(process.argv[1]); new Installation(process.argv[2], process.argv[3], 'python').publishCli();"
& $env:FOCUS_FIXTURE_NODE -e $publisher (Join-Path (Split-Path -Parent $scriptPath) 'managed-update.cjs') $homePath $appPath
if ($LASTEXITCODE -ne 0) { throw 'CLI publication failed' }
'@
}
$functions = $ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $true)
foreach ($fn in ($functions | Sort-Object { $_.Extent.StartOffset } -Descending)) {
    if ($replacements.ContainsKey($fn.Name)) {
        $replacement = 'function ' + $fn.Name + ' { ' + $replacements[$fn.Name] + ' }'
        $source = $source.Substring(0, $fn.Extent.StartOffset) + $replacement + $source.Substring($fn.Extent.EndOffset)
    }
}
function Invoke-RestMethod {
    if ($env:FOCUS_FIXTURE_SELECTED -eq 'dev') { throw 'Dev bootstrap queried Releases' }
    [PSCustomObject]@{ draft=$false; prerelease=$false; published_at='today'; tag_name='v0.2.0' }
}
if ($env:FOCUS_FIXTURE_EXPLICIT -eq 'yes') {
    & ([scriptblock]::Create($source)) -Dev:($env:FOCUS_FIXTURE_SELECTED -eq 'dev') -Stable:($env:FOCUS_FIXTURE_SELECTED -eq 'stable')
} else { & ([scriptblock]::Create($source)) }
`;

for (const [scenario, installed, selected, explicit] of [
  ["fresh-dev", null, "dev", true],
  ["legacy", { branch: "main" }, "dev", false],
  ["stable-default", { channel: "stable" }, "stable", false],
  ["dev-default", { channel: "dev" }, "dev", false],
  ["switch-dev", { channel: "stable" }, "dev", true],
  ["old-release", { channel: "dev" }, "stable", true],
  ["failed-upgrade", { branch: "main" }, "dev", true],
]) {
  test(`Windows bootstrap: ${scenario}`, { skip: process.platform !== "win32" }, t => {
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), "focus-bootstrap-"));
    t.after(() => fs.rmSync(profile, { recursive: true, force: true }));
    const home = path.join(profile, ".focus"), app = path.join(home, "app");
    if (installed) {
      for (const directory of [app, path.join(home, "runtime/cli")]) fs.mkdirSync(directory, { recursive: true });
      fs.writeFileSync(path.join(app, "code"), "previous code");
      fs.writeFileSync(path.join(home, "runtime/cli/managed-update.cjs"), "old updater");
      fs.writeFileSync(path.join(home, "config.yaml"), "my config");
      fs.writeFileSync(path.join(home, ".env"), "my env");
      fs.writeFileSync(path.join(home, "extensions_config.json"), "my extensions");
      fs.writeFileSync(path.join(home, "install.json"), JSON.stringify({ managed: true, repository: "https://github.com/Leeminjing/Focus.git", appPath: app, ...installed }));
    }
    const result = cp.spawnSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-EncodedCommand", Buffer.from(harness, "utf16le").toString("base64")], {
      encoding: "utf8", windowsHide: true, timeout: 30000,
      env: { ...process.env, FOCUS_CHANNEL: "", FOCUS_FIXTURE_ROOT: root, FOCUS_FIXTURE_HOME: profile,
        FOCUS_FIXTURE_CASE: scenario, FOCUS_FIXTURE_SELECTED: selected, FOCUS_FIXTURE_EXPLICIT: explicit ? "yes" : "no", FOCUS_FIXTURE_NODE: process.execPath },
    });
    if (scenario === "failed-upgrade") {
      assert.notEqual(result.status, 0);
      assert.match(result.stderr, /fixture transaction failure/);
      assert.ok(!fs.existsSync(path.join(home, "bin/focus.cmd")));
    } else {
      assert.equal(result.status, 0, result.stderr || result.stdout);
      assert.match(fs.readFileSync(path.join(home, "bin/focus.cmd"), "utf8"), /cli-launch\.cjs/);
      const { generation } = JSON.parse(fs.readFileSync(path.join(home, "runtime/cli/current.json")));
      assert.ok(fs.existsSync(path.join(home, "runtime/cli/generations", generation, "cli-protocol.json")));
    }
    if (installed) {
      for (const [name, value] of [["config.yaml", "my config"], [".env", "my env"], ["extensions_config.json", "my extensions"], ["runtime/cli/managed-update.cjs", "old updater"]]) {
        assert.equal(fs.readFileSync(path.join(home, name), "utf8"), value);
      }
    }
    assert.deepEqual(fs.readdirSync(path.join(home, "staging")), []);
  });
}
