/*
 * 本文件对外提供 F22 高完成度交互系统静态守卫。输入为桌面 HTML、语义 CSS、会话 renderer、
 * 本地图标资产与依赖清单，输出为无半框选中态、紧凑事件序列、专业离线图标、统一 motion、
 * reduced-motion 和同源/零依赖断言；示例：`node f22-premium-ui.test.cjs`。
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const read = relative => fs.readFileSync(path.join(root, relative), "utf8");
const rule = (source, selector) => {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return source.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`))?.[1] || "";
};

const index = read("desktop/index.html");
const app = read("desktop/app.js");
const eventsJs = read("desktop/conversation-events.js");
const legacyStyles = read("desktop/styles.css");
const tokens = read("desktop/styles/tokens.css");
const base = read("desktop/styles/base.css");
const components = read("desktop/styles/components.css");
const shell = read("desktop/styles/shell.css");
const views = read("desktop/styles/views.css");
const events = read("desktop/styles/conversation-events.css");
const packageJson = JSON.parse(read("desktop/package.json"));

assert.match(index, /styles\/icons\.css/);
const icons = read("desktop/styles/icons.css");
assert.match(icons, /mask(?:-image)?\s*:/);
assert.match(icons, /\.ui-icon/);

const license = read("desktop/assets/icons/LICENSE.txt");
assert.match(license, /ISC License/);
assert.match(license, /MIT License/);
for (const icon of ["brain-circuit", "wrench", "terminal", "folder", "search", "file", "file-text", "file-image", "loader-circle", "circle-check", "triangle-alert", "circle-x", "x", "plus", "minus", "package", "trash-2", "grip-vertical", "chevron-left", "chevron-right"]) {
  assert.ok(fs.existsSync(path.join(root, "desktop", "assets", "icons", `${icon}.svg`)), `缺少本地图标 ${icon}`);
}

assert.equal((index.match(/class="app-nav-item"/g) || []).length, 5);
assert.doesNotMatch(index, /app-nav-icon|icon-(?:house|map|layers|bot|plug)/, "左侧导航仍包含图标或图标占位");
assert.doesNotMatch(index, /workspace-context|workspaceKicker|workspaceTitle|workspaceMeta/, "主工作区仍渲染与顶栏重复的大型 Workspace Header");
assert.doesNotMatch(app, /function viewHeading|workspaceKicker|workspaceTitle|workspaceMeta/, "renderer 仍维护已删除的第二套标题状态");
assert.doesNotMatch(shell, /\.workspace-context\s*\{/, "壳层仍保留第二套标题样式");
assert.match(rule(shell, ".app-workspace"), /grid-template-rows:\s*minmax\(0, 1fr\)/, "主工作区没有直接把唯一网格行交给页面内容");
assert.doesNotMatch(shell, /\.app-nav-label\s*\{[^}]*clip-path:\s*inset\(50%\)/s, "窄屏仍会隐藏导航文字");
for (const removedIcon of ["house", "map", "layers", "bot", "plug"]) {
  assert.ok(!fs.existsSync(path.join(root, "desktop", "assets", "icons", `${removedIcon}.svg`)), `已删除的导航图标仍存在：${removedIcon}`);
}
assert.doesNotMatch(app, /context-family|context-root-label|context-derived-(?:list|heading|items)/, "Context rail 仍包含额外根容器或分区结构");
assert.match(app, /style="--context-depth:\$\{depth\}"/);
assert.match(app, /context-rail-meta/);
assert.doesNotMatch(app, /context-tree-(?:root|children|node)|context-current-marker|context-rail-lineage/, "Context rail 又引入了树线、层级胶囊或重复父来源");
assert.match(legacyStyles, /\.context-rail-card,\s*\n\.context-rail-add\s*\{[^}]*border:\s*1px solid[^;]*;[^}]*border-radius:[^;]+;[^}]*text-align:\s*left/s);
assert.match(legacyStyles, /\.context-rail-card\.is-current\s*\{[^}]*border-color:\s*var\(--border-accent[^}]*background:\s*var\(--surface-accent/s);
assert.match(views, /\.app-inspector \.context-rail-card\.is-current\s*\{[^}]*border-color:\s*var\(--border-accent\)/s);

for (const [name, source] of [["index", index], ["app", app], ["events", eventsJs]]) {
  assert.doesNotMatch(source, /[⌂⊞⑂◎◇↳⠿🖼📕📘📄📦🗑]/u, `${name} 仍包含字符或 emoji 图标`);
}
assert.doesNotMatch(read("desktop/context-editor.js"), /⠿/u);
assert.doesNotMatch(read("plugins/spatial-patrol/desktop/viewer.js"), /[◀▶＋−]/u);

for (const [name, body] of [
  ["navigation", rule(shell, '.app-nav-item[aria-current="page"]')],
  ["inspector tab", rule(shell, '.inspector-tabs button[aria-selected="true"]')],
  ["context", rule(views, ".app-inspector .context-rail-card.is-current")],
  ["task", rule(views, ".task-card-shell:focus-within")],
  ["plugin", rule(views, ".plugin-card.is-selected")],
]) {
  assert.ok(body, `缺少 ${name} 选中规则`);
  assert.doesNotMatch(body, /box-shadow\s*:\s*inset|border-(left|bottom)[^;]*(accent|primary)/, `${name} 仍使用强调色半框`);
}

assert.match(app, /conversation-event-sequence/);
assert.match(app, /reconcileConversationMarkup/);
assert.match(eventsJs, /data-event-key/);
assert.match(events, /\.conversation-event-sequence/);
assert.match(rule(events, ".conversation-event-sequence"), /gap:\s*2px/);
assert.match(rule(events, ".conversation-event > summary"), /min-height:\s*(28|30|32)px/);
assert.match(eventsJs, /ui-icon/);

for (const token of ["--motion-instant", "--motion-fast", "--motion-standard", "--motion-deliberate", "--ease-out"]) {
  assert.match(tokens, new RegExp(token));
}
assert.match(components, /button:active[^\{]*\{[^}]*transform:/s);
assert.doesNotMatch([base, components, shell, views, events, icons].join("\n"), /transition\s*:\s*all\b/);
assert.match(base, /prefers-reduced-motion:\s*reduce/);
assert.match(base, /\(update:\s*slow\)/);

assert.deepEqual(Object.keys(packageJson.dependencies || {}), ["electron"]);
assert.match(read("desktop/main.cjs"), /loadURL\(`\$\{apiBase\}\/desktop\/`\)/);
assert.match(app, /location\.origin/);

assert.match(app, /<div class="map-toolbar"><button class="soldier-source"/);
assert.doesNotMatch(app, /map-toolbar[^\n]*TASK MAP|map-toolbar[^\n]*按工作区与根 Context 浏览/);
assert.match(app, /<header class="draft-heading"><span class="ui-meta">来源 checkpoint/);
assert.doesNotMatch(app, /draft-heading[^\n]*AGENT DRAFT|draft-heading[^\n]*小兵草稿/);
assert.match(app, /<header class="compression-heading">\s*<p class="compression-usage">/s);
assert.doesNotMatch(app, /compression-heading[\s\S]{0,240}(?:CONTEXT COMPRESSION|<h1>上下文压缩<\/h1>)/);
assert.match(views, /@media \(max-width: 720px\)[\s\S]*\.context-editor-view\s*\{[\s\S]*grid-template-columns:\s*1fr/s);
assert.match(views, /@media \(max-width: 720px\)[\s\S]*\.plugins-workbench\s*\{\s*grid-template-columns:\s*1fr/s);
assert.match(views, /@media \(max-width: 1100px\)[\s\S]*\.focus-shell:has\(\.file-panel\)\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\) !important/s);
assert.match(views, /\.focus-shell:has\(\.file-panel\) > \.focus-view\s*\{\s*display:\s*none/s);
assert.match(rule(views, ".task-card"), /min-height:\s*0/);

console.log("f22-premium-ui: 无半框、紧凑执行序列、单一顶栏任务上下文、高缩放单工作面与同源零依赖守卫通过");
