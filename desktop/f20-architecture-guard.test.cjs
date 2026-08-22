/*
 * 本文件对外提供 F20 前端架构静态守卫。输入为桌面清单、Electron 主进程、Gateway 挂载、
 * HTML 与渲染入口源码，输出为零新增依赖、零外部 CDN、同一 loopback Origin 和无 CORS/代理
 * 的断言结果；工作流只读取仓库文件并在约束被破坏时退出失败。示例：`node f20-architecture-guard.test.cjs`。
 */
"use strict";

const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const read = relative => fs.readFileSync(path.join(root, relative), "utf8");

const packageJson = JSON.parse(read("desktop/package.json"));
assert.deepStrictEqual(packageJson.dependencies, { electron: "^43.2.0" });
assert.strictEqual(packageJson.devDependencies, undefined);

const html = read("desktop/index.html");
const externalAssets = [...html.matchAll(/<(?:script|link)\b[^>]+(?:src|href)=["'](https?:\/\/[^"']+)/gi)];
assert.deepStrictEqual(externalAssets.map(match => match[1]), []);
const styleOrder = [
  "./styles.css",
  "./skill-picker.css",
  "./styles/tokens.css",
  "./styles/base.css",
  "./styles/components.css",
  "./styles/shell.css",
  "./styles/views.css",
  "./styles/conversation-events.css",
].map(value => html.indexOf(value));
assert.ok(styleOrder.every(index => index >= 0));
assert.deepStrictEqual([...styleOrder].sort((a, b) => a - b), styleOrder);

const tokens = read("desktop/styles/tokens.css");
for (const token of ["--surface-canvas", "--text-primary", "--border-default", "--accent-primary", "--status-danger", "--space-4", "--radius-card", "--motion-standard", "--focus-plugin-surface", "--focus-plugin-surface-inverse", "--focus-plugin-success", "--focus-plugin-warning"]) {
  assert.match(tokens, new RegExp(token));
}
assert.match(read("desktop/styles/base.css"), /prefers-reduced-motion:\s*reduce/);
const legacyStyles = read("desktop/styles.css");
assert.doesNotMatch(legacyStyles, /\.plugins-panels|\.plugins-interfaces|\.plugin-status-badge/);
assert.doesNotMatch(legacyStyles, /\.materials(?:\s|\.|\{)|\.material-meta|\.material-toggle/);

const spatialStyle = read("plugins/spatial-patrol/desktop/style.css");
const eyesStyle = read("plugins/dsh-eyes/desktop/style.css");
assert.doesNotMatch(spatialStyle, /\n\.(?:rail-wrap|rail-resizer|message-file-cards|file-card|panel-resizer)\b/);
assert.doesNotMatch(spatialStyle, /var\(--(?!focus-plugin)/);
assert.doesNotMatch(eyesStyle, /var\(--(?!focus-plugin)/);
assert.match(spatialStyle, /\.file-panel[\s\S]*\.spatial-viewer/);
assert.match(eyesStyle, /\.dsh-eyes-paste-preview/);

const main = read("desktop/main.cjs");
assert.match(main, /const apiBase = `http:\/\/127\.0\.0\.1:\$\{port\}`/);
assert.match(main, /process\.env\.FOCUS_DESKTOP_API = apiBase/);
assert.match(main, /await mainWindow\.loadURL\(`\$\{apiBase\}\/desktop\/`\)/);
assert.match(main, /mainWindow\.show\(\);[\s\S]*closeSplashWindow\(\)/);
assert.match(main, /setWindowOpenHandler/);
assert.match(main, /will-navigate/);
assert.match(main, /new URL\(target\)\.origin === new URL\(apiBase\)\.origin/);
assert.match(main, /focus:open-external/);

const preload = read("desktop/preload.cjs");
assert.match(preload, /apiBase:\s*process\.env\.FOCUS_DESKTOP_API/);
assert.match(preload, /openExternal:\s*url => ipcRenderer\.invoke\("focus:open-external", url\)/);
assert.doesNotMatch(preload, /fetch\(|EventSource|proxy/i);

const renderer = read("desktop/app.js");
assert.match(renderer, /location\.origin/);
assert.match(renderer, /fetch\(`\$\{runtime\.apiBase\}\$\{path\}`/);
assert.match(renderer, /new EventSource\(`\$\{runtime\.apiBase\}\/desktop\/api\/runs\//);
assert.match(renderer, /script\.onerror = \(\) => \{[\s\S]*pluginScriptAssets\.delete\(src\)[\s\S]*console\.error\("插件脚本加载失败:"[\s\S]*resolve\(\)/);
assert.match(renderer, /link\.onerror = \(\) => \{[\s\S]*pluginStyleAssets\.delete\(href\)[\s\S]*console\.error\("插件样式加载失败:"/);
assert.match(renderer, /filter\(plugin => plugin\.status === "active"\)/);
assert.match(html, /<script src="\.\/conversation-events\.js"><\/script>[\s\S]*<script src="\.\/app\.js"><\/script>/);

const gateway = read("backend/app/gateway/app.py");
const desktopApp = read("backend/app/desktop/app.py");
assert.match(gateway, /mount_desktop\(app\)/);
assert.match(desktopApp, /app\.mount\("\/desktop"/);
assert.match(desktopApp, /f"\/plugins\/\{name\}\/desktop"/);
assert.doesNotMatch(gateway + desktopApp, /CORSMiddleware|allow_origins|proxy/i);

console.log("f20-architecture-guard: 同源拓扑、零 CORS/代理、零新增依赖与零外部 CDN 通过");
