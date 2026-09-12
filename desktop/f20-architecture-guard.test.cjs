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

const shellStyles = read("desktop/styles/shell.css");
const cssRule = (source, selector) => {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = source.match(new RegExp(`(?:^|\\})\\s*${escaped}\\s*\\{([^}]+)\\}`));
  assert.ok(match, `缺少 CSS 规则 ${selector}`);
  return match[1];
};
const appShellRule = cssRule(shellStyles, ".app-shell");
assert.match(appShellRule, /display:\s*flex/);
assert.match(appShellRule, /flex-direction:\s*row/);
assert.doesNotMatch(appShellRule, /grid-template-columns/);
// 尺寸唯一来源：.app-shell 携带三栏默认宽度，供用户拖拽用内联自定义属性覆盖。
assert.match(appShellRule, /--shell-nav-width:\s*[^;]+/);
assert.match(appShellRule, /--inspector-width:\s*[^;]+/);

const navigationRule = cssRule(shellStyles, ".app-navigation");
const workspaceRule = cssRule(shellStyles, ".app-workspace");
const inspectorRule = cssRule(shellStyles, ".app-inspector");
assert.match(navigationRule, /flex:\s*0\s+1\s+var\(--shell-nav-width\)/);
// 导航可拖拽到 0（折叠）：min-width 必须允许收缩到 0，而不是锁死在折叠宽度。
assert.match(navigationRule, /min-width:\s*0/);
assert.match(navigationRule, /max-width:\s*var\(--shell-nav-max-width\)/);
assert.match(workspaceRule, /flex:\s*1\s+1\s+0/);
assert.match(workspaceRule, /min-width:\s*var\(--workspace-min-width\)/);
assert.match(inspectorRule, /flex:\s*0\s+1\s+var\(--inspector-width\)/);
assert.match(inspectorRule, /min-width:\s*var\(--inspector-min-width\)/);
assert.match(inspectorRule, /max-width:\s*var\(--inspector-max-width\)/);
assert.match(shellStyles, /\.app-inspector\[hidden\]\s*\{\s*display:\s*none/);
// 三栏 resizer 手柄：导航↔工作区、工作区↔检查器。
assert.match(shellStyles, /\.shell-resizer\s*\{[^}]*touch-action:\s*none/);
// 拖拽手柄无痕：不得有 .shell-resizer:hover 背景高亮（保留 cursor: col-resize 提示）。
assert.doesNotMatch(shellStyles, /\.shell-resizer:hover\s*\{[^}]*background:/, "拖拽手柄仍显示 :hover 背景高亮线");

// 头部折叠触发器：logo 存在、保留图片、action 已改为折叠开关；独立的「收起导航」按钮样式被移除。
assert.match(html, /class="app-mark"[^>]*data-action="toggle-nav-collapse"/);
assert.match(html, /class="app-mark"[^>]*aria-controls="appNavigation"/);
assert.match(html, /class="app-mark"[\s\S]*assets\/focus-icon\.png/, "应用头部没有保留用户指定的品牌 Logo");
assert.doesNotMatch(shellStyles, /\.app-nav-toggle(?:\s|\.|\{)/, "壳层仍保留已删除的「收起导航」按钮样式");
// 头部网格收敛为 4 列（删除折叠按钮后恢复断言），而非过渡态的 5 列。
assert.match(shellStyles, /\.app-header\s*\{[^}]*grid-template-columns:\s*auto\s+minmax\(0,\s*1fr\)\s+auto\s+auto/);
assert.doesNotMatch(shellStyles, /\.app-header\s*\{[^}]*grid-template-columns:\s*auto\s+auto\s+minmax\(0,\s*1fr\)\s+auto\s+auto/, ".app-header 仍为 5 列（遗留折叠按钮占用的一格）");
// logo 无痕：.app-mark 不得有 :hover 背景（可点击性仅由 cursor 与键盘焦点样式提示）。
assert.doesNotMatch(shellStyles, /\.app-mark:hover\s*\{[^}]*background:/, ".app-mark 仍显示 :hover 背景");
assert.match(shellStyles, /\.app-mark\s*\{[^}]*cursor:\s*pointer/, ".app-mark 缺少 cursor: pointer 可点击提示");

const mediumLayout = shellStyles.match(/@media \(max-width:\s*1100px\)\s*\{([\s\S]*?)@media \(max-width:\s*900px\)/)?.[1] || "";
// 1100px 断点只覆盖默认宽度自定义属性，不再覆盖 flex 简写（避免覆盖用户内联值）。
assert.match(mediumLayout, /\.app-shell\s*\{[^}]*--shell-nav-width:/);
assert.doesNotMatch(mediumLayout, /\.app-navigation\s*\{[^}]*flex:\s*0\s+0/);
assert.doesNotMatch(mediumLayout, /grid-template-columns/);
const narrowLayout = shellStyles.match(/@media \(max-width:\s*900px\)\s*\{([\s\S]*?)@media \(max-width:\s*640px\)/)?.[1] || "";
assert.match(narrowLayout, /\.app-inspector\s*\{[^}]*position:\s*absolute/);
assert.match(narrowLayout, /\.app-inspector\s*\{[^}]*flex:\s*none/);
assert.doesNotMatch(narrowLayout, /grid-template-columns/);

const spatialStyle = read("plugins/spatial-patrol/desktop/style.css");
assert.doesNotMatch(spatialStyle, /\n\.(?:rail-wrap|rail-resizer|message-file-cards|file-card|panel-resizer)\b/);
assert.doesNotMatch(spatialStyle, /var\(--(?!focus-plugin)/);
assert.match(spatialStyle, /\.file-panel[\s\S]*\.spatial-viewer/);

const main = read("desktop/main.cjs");
assert.match(main, /const apiBase = `http:\/\/127\.0\.0\.1:\$\{port\}`/);
assert.match(main, /process\.env\.FOCUS_DESKTOP_API = apiBase/);
assert.match(main, /await mainWindow\.loadURL\(`\$\{apiBase\}\/desktop\/`\)/);
assert.match(main, /mainWindow\.show\(\);[\s\S]*closeSplashWindow\(\)/);
assert.match(main, /setWindowOpenHandler/);
assert.match(main, /will-navigate/);
assert.match(main, /new URL\(target\)\.origin === new URL\(apiBase\)\.origin/);
assert.match(main, /focus:open-external/);
// 整页缩放：主进程是缩放权威，经 IPC 设置/读取，并对级别做边界钳制。
assert.match(main, /ipcMain\.handle\("focus:set-zoom-level"/);
assert.match(main, /ipcMain\.handle\("focus:get-zoom-level"/);
assert.match(main, /setZoomLevel\(/);
assert.match(main, /ZOOM_LEVEL_MIN\s*=\s*-3/);
assert.match(main, /ZOOM_LEVEL_MAX\s*=\s*5/);
// 窗口控制覆盖层：带高随缩放同步，避免原生按钮与（随缩放的）页面头部错位遮挡。
assert.match(main, /TITLEBAR_BASE_HEIGHT\s*=\s*56/);
assert.match(main, /setTitleBarOverlay\(/);
assert.doesNotMatch(main, /titleBarOverlay\s*=\s*\{[^}]*height:\s*56\b/, "titleBarOverlay 高度仍是裸字面量，未复用 TITLEBAR_BASE_HEIGHT");
// 同步逻辑必须挂在缩放处理路径上：同一 handler 内 setZoomLevel 之后调用。
assert.match(main, /focus:set-zoom-level"[\s\S]*syncTitleBarOverlay\(/);
// 原生带高必须等于页面头部带高（tokens.css --shell-header-height），防止两处漂移。
const titlebarBaseHeight = main.match(/TITLEBAR_BASE_HEIGHT\s*=\s*(\d+)/)?.[1];
const shellHeaderHeight = tokens.match(/--shell-header-height:\s*(\d+)px/)?.[1];
assert.ok(titlebarBaseHeight, "main.cjs 缺少 TITLEBAR_BASE_HEIGHT 常量");
assert.ok(shellHeaderHeight, "tokens.css 缺少 --shell-header-height");
assert.strictEqual(titlebarBaseHeight, shellHeaderHeight, "原生覆盖层带高与页面头部带高不一致");

const preload = read("desktop/preload.cjs");
assert.match(preload, /apiBase:\s*process\.env\.FOCUS_DESKTOP_API/);
assert.match(preload, /openExternal:\s*url => ipcRenderer\.invoke\("focus:open-external", url\)/);
assert.doesNotMatch(preload, /fetch\(|EventSource|proxy/i);
// 缩放桥：仍只经 ipcRenderer.invoke，不引入第二套 Electron 访问机制。
assert.match(preload, /setZoomLevel:\s*level => ipcRenderer\.invoke\("focus:set-zoom-level", level\)/);
assert.match(preload, /getZoomLevel:\s*\(\) => ipcRenderer\.invoke\("focus:get-zoom-level"\)/);

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
assert.match(html, /<script src="\.\/conversation-events\.js"><\/script>[\s\S]*<script src="\.\/context-curator-presentation\.js\?v=20260906a"><\/script>[\s\S]*<script src="\.\/patrol-presence\.js\?v=20260905b"><\/script>[\s\S]*<script src="\.\/patrol-avatar\.js\?v=20260905b"><\/script>[\s\S]*<script src="\.\/app\.js\?v=20260912a"><\/script>/);
assert.match(html, /<link rel="stylesheet" href="\.\/styles\/patrol-avatar\.css\?v=20260905b">/);
assert.match(renderer, /patrol_avatar_positions/);
assert.match(renderer, /FocusPatrolAvatar/);
assert.match(renderer, /FocusPatrolPresence/);
assert.match(renderer, /action\.id === "quick-curate"[\s\S]*quickDeployContextCurator\(task\.task_id\)[\s\S]*action\.id === "configure"[\s\S]*openDraft\(task\.task_id\)[\s\S]*action\.id === "details"[\s\S]*openAgentDetails\(avatar\.agent_id\)/);
assert.match(patrolPresence, /STANDBY_AVATAR_ID = "__standby__"/);
assert.doesNotMatch(patrolPresence, /fetch\(|EventSource|XMLHttpRequest|\/desktop\/api\//);

// 整页缩放：渲染器负责快捷键与持久化，经 preload 桥调用主进程；播报区无视觉百分比。
assert.match(renderer, /ZOOM_LEVEL_KEY/);
assert.match(renderer, /function normalizeZoomLevel/);
assert.match(renderer, /function applyZoomLevel/);
assert.match(renderer, /event\.ctrlKey \|\| event\.metaKey/);
assert.match(renderer, /focusDesktop\?\.setZoomLevel/);
assert.match(html, /id="zoomAnnouncement"[^>]*class="visually-hidden"[^>]*aria-live="polite"/);

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
