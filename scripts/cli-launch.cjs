/* Permanent dispatcher. The pointer is changed only by a protected update. */
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const cp = require("node:child_process");

function cliDirectory(root) {
  const pointer = path.join(root, "current.json");
  if (!fs.existsSync(pointer)) return root; // pre-generation installation
  const { generation } = JSON.parse(fs.readFileSync(pointer, "utf8"));
  if (!/^cli-[a-zA-Z0-9-]+$/.test(generation)) throw new Error("Invalid Focus CLI generation.");
  return path.join(root, "generations", generation);
}

function launch(root, args) {
  const directory = cliDirectory(root);
  const windows = process.platform === "win32";
  const result = cp.spawnSync(windows ? "powershell.exe" : "sh", windows
    ? ["-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path.join(directory, "focus.ps1"), ...args]
    : [path.join(directory, "focus.sh"), ...args], {
    stdio: "inherit", windowsHide: true, env: process.env,
  });
  if (result.error) throw result.error;
  return result.status ?? 1;
}
module.exports = { cliDirectory, launch };
if (require.main === module) process.exitCode = launch(__dirname, process.argv.slice(2));
