/*
 * 本文件对外提供 F20 前端架构静态守卫。输入为桌面清单、Electron 主进程、Gateway 挂载、
 * HTML、渲染入口与 Patrol presence 源码，输出为零新增依赖、零外部 CDN、同一 loopback
 * Origin、无 CORS/代理和无待命后端副作用的断言结果；工作流只读取仓库文件并在约束被
 * 破坏时退出失败。示例：`node f20-architecture-guard.test.cjs`。
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
  "./styles/patrol-avatar.css",
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
const patrolPresence = read("desktop/patrol-presence.js");
assert.doesNotMatch(html, /id=["']agentDialog["']|class=["'][^"']*agent-dialog/);
assert.doesNotMatch(renderer, /agentDialog|renderAgentDialog|agentContinueInput/);
assert.match(renderer, /agentDetails:\s*\{\s*agentId:\s*null/);
assert.match(renderer, /openAgentDetails\(agentId\)[\s\S]*openInspector\("agents"/);
assert.match(renderer, /location\.origin/);
assert.match(renderer, /fetch\(`\$\{runtime\.apiBase\}\$\{path\}`/);
assert.match(renderer, /new EventSource\(`\$\{runtime\.apiBase\}\/desktop\/api\/runs\//);
assert.match(renderer, /script\.onerror = \(\) => \{[\s\S]*pluginScriptAssets\.delete\(src\)[\s\S]*console\.error\("插件脚本加载失败:"[\s\S]*resolve\(\)/);
assert.match(renderer, /link\.onerror = \(\) => \{[\s\S]*pluginStyleAssets\.delete\(href\)[\s\S]*console\.error\("插件样式加载失败:"/);
assert.match(renderer, /filter\(plugin => plugin\.status === "active"\)/);
assert.match(html, /<script src="\.\/conversation-events\.js"><\/script>[\s\S]*<script src="\.\/context-curator-presentation\.js\?v=20260906a"><\/script>[\s\S]*<script src="\.\/patrol-presence\.js\?v=20260905b"><\/script>[\s\S]*<script src="\.\/patrol-avatar\.js\?v=20260905b"><\/script>[\s\S]*<script src="\.\/app\.js\?v=20260906b"><\/script>/);
assert.match(html, /<link rel="stylesheet" href="\.\/styles\/patrol-avatar\.css\?v=20260905b">/);
assert.match(renderer, /patrol_avatar_positions/);
assert.match(renderer, /FocusPatrolAvatar/);
assert.match(renderer, /FocusPatrolPresence/);
assert.match(renderer, /action\.id === "quick-curate"[\s\S]*quickDeployContextCurator\(task\.task_id\)[\s\S]*action\.id === "configure"[\s\S]*openDraft\(task\.task_id\)[\s\S]*action\.id === "details"[\s\S]*openAgentDetails\(avatar\.agent_id\)/);
assert.match(patrolPresence, /STANDBY_AVATAR_ID = "__standby__"/);
assert.doesNotMatch(patrolPresence, /fetch\(|EventSource|XMLHttpRequest|\/desktop\/api\//);

const migrationDirectory = path.join(root, "backend/packages/harness/focus/persistence/migrations/versions");
const backendPatrolBoundary = [
  read("backend/app/desktop/models.py"),
  read("backend/app/desktop/routes.py"),
  ...fs.readdirSync(migrationDirectory)
    .filter(name => name.endsWith(".py"))
    .map(name => fs.readFileSync(path.join(migrationDirectory, name), "utf8")),
].join("\n");
assert.doesNotMatch(backendPatrolBoundary, /__standby__|FocusPatrolPresence|session-patrol-presence/);

const gateway = read("backend/app/gateway/app.py");
const desktopApp = read("backend/app/desktop/app.py");
assert.match(gateway, /mount_desktop\(app\)/);
assert.match(desktopApp, /app\.mount\("\/desktop"/);
assert.match(desktopApp, /f"\/plugins\/\{name\}\/desktop"/);
assert.doesNotMatch(gateway + desktopApp, /CORSMiddleware|allow_origins|proxy/i);

console.log("f20-architecture-guard: 同源拓扑、零 CORS/代理、零新增依赖与零外部 CDN 通过");
