/* Validates the local-only startup/branding contract without launching services. */
"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const desktopDir = __dirname;
const read = relative => fs.readFileSync(path.join(desktopDir, relative));
const text = relative => read(relative).toString("utf8");

const png = read("assets/focus-icon.png");
const ico = read("assets/focus-icon.ico");
assert.equal(
  crypto.createHash("sha256").update(png).digest("hex"),
  "d70e8fa1c4d150b438c88701fd0a1ba77488da0df86238d651d78b91371d67de",
  "the bundled PNG must be the user-supplied icon without alteration",
);
assert.equal(png.subarray(1, 4).toString("ascii"), "PNG");
assert.ok(png.readUInt32BE(16) >= 256 && png.readUInt32BE(20) >= 256, "PNG needs enough resolution for desktop branding");
assert.equal(ico.readUInt16LE(0), 0);
assert.equal(ico.readUInt16LE(2), 1);
assert.ok(ico.readUInt16LE(4) >= 6, "ICO should contain multiple Windows icon sizes");

const index = text("index.html");
assert.match(index, /rel="icon"[^>]+assets\/focus-icon\.png/);
assert.doesNotMatch(index, /class="brand"|<header class="app-header">[\s\S]*<span>Focus<\/span>/);
assert.match(index, /class="app-mark"[\s\S]*assets\/focus-icon\.png/);

const shellCss = text("styles/shell.css");
assert.doesNotMatch(shellCss, /\.brand(?:\s|[.#:{])/);
assert.match(shellCss, /\.app-mark\s*\{/);
assert.match(shellCss, /-webkit-app-region:\s*drag/);
assert.match(shellCss, /-webkit-app-region:\s*no-drag/);
assert.match(shellCss, /titlebar-area-width/);

const splash = text("splash.html");
const splashJs = text("splash.js");
const splashCss = text("styles/splash.css");
assert.match(splash, /Content-Security-Policy/);
assert.doesNotMatch(splash, /https?:\/\//);
assert.match(splash, /role="status" aria-live="polite"/);
assert.match(splash, /assets\/focus-icon\.png/);
assert.match(splash, /aria-label="Focus 正在启动"/);
assert.doesNotMatch(splash, /splash-copy|splash-kicker|LOCAL AGENT WORKSPACE|<h1[^>]*>Focus<\/h1>/);
assert.doesNotMatch(splashCss, /\.splash-(?:copy|kicker)|\.splash-copy\s+h1/);
assert.match(splashJs, /window\.setFocusSplashStage/);
assert.match(splashJs, /Math\.max\(0, Math\.min\(100/);
assert.match(splashCss, /@keyframes focus-arrive/);
assert.match(splashCss, /@keyframes focus-float/);
assert.match(splashCss, /prefers-reduced-motion: reduce/);

const main = text("main.cjs");
assert.match(main, /focus-icon\.ico/);
assert.match(main, /createSplashWindow\(\)/);
assert.match(main, /titleBarStyle:\s*"hidden"/);
assert.match(main, /titleBarOverlay:\s*\{[\s\S]*height:\s*56[\s\S]*\}/);
assert.match(main, /show: false,[\s\S]*frame: false,[\s\S]*loadFile\(path\.join\(desktopDir, "splash\.html"\)\)/);
assert.ok(main.indexOf("await createSplashWindow()") < main.indexOf("const pythonRuntime = resolvePythonRuntime()"));
assert.ok(main.indexOf("await mainWindow.loadURL") < main.indexOf("mainWindow.show()"));
assert.ok(main.indexOf("mainWindow.show()") < main.lastIndexOf("closeSplashWindow()"));
assert.match(main, /import alembic, uvicorn/);
assert.match(main, /where\.exe/);
assert.match(main, /app\.setAppUserModelId\("Focus\.Desktop"\)/);
assert.match(main, /FOCUS_DISABLE_HARDWARE_ACCELERATION[\s\S]*app\.disableHardwareAcceleration\(\)/);

const packageJson = JSON.parse(text("package.json"));
assert.deepEqual(Object.keys(packageJson.dependencies || {}), ["electron"], "branding/startup must not add dependencies");

console.log("startup-ui: 原始图标、Windows ICO、启动阶段、动画降级与零新增依赖通过");
