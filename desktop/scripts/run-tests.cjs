"use strict";
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const desktopDir = path.resolve(__dirname, "..");
const tests = ["src", "tests"].flatMap(directory =>
  fs.readdirSync(path.join(desktopDir, directory), { recursive: true })
    .filter(file => /\.test\.(?:cjs|js)$/.test(file))
    .map(file => path.join(directory, file)),
).sort();
const result = spawnSync(process.execPath, ["--test", "--test-concurrency=4", ...process.argv.slice(2), ...tests], {
  cwd: desktopDir, stdio: "inherit", windowsHide: true,
});
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
