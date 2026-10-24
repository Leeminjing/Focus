"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const cp = require("node:child_process");
const root = path.resolve(__dirname, "../../..");
const scenarios = [
  [["update"], "selected:"],
  [["update", "--dev"], "selected:--dev"],
  [["update", "--stable"], "selected:--stable"],
  [["update", "--dev", "--stable"], null],
  [["update", "--other"], null],
  [["update", "--DEV"], null],
  [["update", "--dev", "--dev"], null],
];

for (const [args, expected] of scenarios) {
  test(`Windows command routing: ${args.join(" ")}`, { skip: process.platform !== "win32" }, () => {
    const script = String.raw`
$source = Get-Content -Raw -LiteralPath $env:FOCUS_FIXTURE_CLI
$source = $source.Replace('$PSScriptRoot', '(Split-Path -Parent $env:FOCUS_FIXTURE_CLI)')
$ast = [System.Management.Automation.Language.Parser]::ParseInput($source, [ref]$null, [ref]$null)
$fn = $ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Update-Focus'}, $false)[0]
$body = 'function Update-Focus { param([string[]]$UpdateArguments = @()); Write-Output ("selected:" + ($UpdateArguments -join ",")) }'
$source = $source.Substring(0, $fn.Extent.StartOffset) + $body + $source.Substring($fn.Extent.EndOffset)
$cliArgs = ConvertFrom-Json $env:FOCUS_FIXTURE_ARGS
& ([scriptblock]::Create($source)) @cliArgs
`;
    const result = cp.spawnSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-EncodedCommand", Buffer.from(script, "utf16le").toString("base64")], {
      encoding: "utf8", windowsHide: true,
      env: { ...process.env, FOCUS_FIXTURE_CLI: path.join(root, "scripts/focus.ps1"), FOCUS_FIXTURE_ARGS: JSON.stringify(args) },
    });
    if (expected !== null) { assert.equal(result.status, 0, result.stderr); assert.match(result.stdout, new RegExp(expected)); }
    else { assert.notEqual(result.status, 0); assert.match(result.stderr, /Usage/); }
  });
}

const gitPath = cp.spawnSync("git", ["--exec-path"], { encoding: "utf8" }).stdout?.trim();
const shell = process.platform === "win32" ? path.resolve(gitPath, "../../../bin/sh.exe") : "sh";
const source = fs.readFileSync(path.join(root, "scripts/focus.sh"), "utf8");
const offset = source.indexOf('if [ "${1:-}" = "doctor" ]; then');
assert.ok(offset > 0);
const script = source.slice(0, offset) + '\nuname() { printf Darwin; }\nupdate_focus() { printf "selected:%s\\n" "$*"; }\n' + source.slice(offset);
for (const [args, expected] of scenarios) {
  test(`POSIX command routing: ${args.join(" ")}`, () => {
    const result = cp.spawnSync(shell, ["-c", script, path.join(root, "scripts/focus.sh"), ...args], { encoding: "utf8", windowsHide: true });
    if (expected !== null) { assert.equal(result.status, 0, result.stderr); assert.match(result.stdout, new RegExp(expected)); }
    else { assert.notEqual(result.status, 0); assert.match(result.stderr, /Usage/); }
  });
}
