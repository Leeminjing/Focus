const assert = require("node:assert/strict");
const { test } = require("node:test");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const fixture = String.raw`
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$FocusHome = $env:FOCUS_TEST_HOME
$AppDir = Join-Path $FocusHome 'app'
$DesktopDir = Join-Path $AppDir 'desktop'
$LocksDir = Join-Path $FocusHome 'locks'
$ExpectedRepository = 'https://github.com/Leeminjing/Focus.git'
$ExpectedBranch = 'main'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:FOCUS_TEST_SCRIPT, [ref]$null, [ref]$null)
$ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $false) | ForEach-Object { Invoke-Expression $_.Extent.Text }
function Assert-ManagedInstall {}
function Get-ApplicationPath { param($Name) 'fixture.exe' }
function Get-CimInstance {
    if ($env:FOCUS_TEST_CASE -eq 'direct-electron') {
        [PSCustomObject]@{ ExecutablePath = (Join-Path $DesktopDir 'node_modules\electron\dist\electron.exe').ToUpperInvariant(); ProcessId = 123 }
    } elseif ($env:FOCUS_TEST_CASE -eq 'other-electron') {
        [PSCustomObject]@{ ExecutablePath = (Join-Path $FocusHome 'other\electron.exe'); ProcessId = 456 }
    }
}
function Invoke-NativeOutput { param($FilePath, $ArgumentList)
    if ($ArgumentList -contains 'get-url') { $ExpectedRepository } else { '1234567890123456789012345678901234567890' }
}
$script:nativeCalls = 0
$script:dependencyProbes = 0
function Test-DependenciesReady { $script:dependencyProbes++; $true }
function Invoke-Native { param($FilePath, $ArgumentList) $script:nativeCalls++ }
function Sync-FocusDependencies {
    try { Start-Focus; throw 'Start was allowed during update' }
    catch { if ($_.Exception.Message -notmatch 'running or being updated') { throw } }
    if ($script:dependencyProbes -ne 0) { throw 'Start probed dependencies during update' }
    if ($env:FOCUS_TEST_CASE -eq 'failed-update') { throw 'fixture dependency failure' }
}

switch ($env:FOCUS_TEST_CASE) {
    'direct-electron' {
        try { Update-Focus; throw 'Update accepted a direct Electron instance' }
        catch { if ($_.Exception.Message -notmatch 'Focus is running.*123') { throw } }
        if ($script:nativeCalls -ne 0) { throw 'Update mutated the install before checking processes' }
    }
    'other-electron' { Assert-FocusIsStopped }
    'cli-running' {
        $held = Open-ExclusiveLock 'running.lock'
        try {
            try { Update-Focus; throw 'Update accepted a running CLI' }
            catch { if ($_.Exception.Message -notmatch 'Focus is running') { throw } }
        } finally { $held.Dispose() }
    }
    'updating' { Update-Focus }
    'failed-update' {
        try { Update-Focus; throw 'Update ignored dependency failure' }
        catch { if ($_.Exception.Message -notmatch 'Focus update failed.*fixture dependency failure') { throw } }
    }
}
foreach ($name in @('running.lock', 'update.lock')) { $released = Open-ExclusiveLock $name; $released.Dispose() }
Write-Output 'update-lock: OK'
`;

for (const scenario of ["direct-electron", "other-electron", "cli-running", "updating", "failed-update"]) {
  test(`Windows updater exclusion: ${scenario}`, { skip: process.platform !== "win32" }, () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), "focus-update-test-"));
    assert.ok(path.resolve(home).startsWith(path.resolve(os.tmpdir()) + path.sep));
    try {
      const result = spawnSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-EncodedCommand",
        Buffer.from(fixture, "utf16le").toString("base64")], {
        windowsHide: true, encoding: "utf8", timeout: 15000,
        env: { ...process.env, FOCUS_TEST_HOME: home, FOCUS_TEST_CASE: scenario,
          FOCUS_TEST_SCRIPT: path.resolve(__dirname, "../scripts/focus.ps1") },
      });
      assert.equal(result.status, 0, result.stderr || result.stdout);
      assert.match(result.stdout, /update-lock: OK/);
    } finally { fs.rmSync(home, { recursive: true, force: true }); }
  });
}
