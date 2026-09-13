/*
 * 本文件对外提供会话页文件预览列的结构守卫。输入为桌面 HTML、壳层样式、预览模块与宿主渲染源码，
 * 输出为「挂载点位置、可见性契约、四栏尺寸与覆盖断点、无插件依赖」四类静态断言；
 * 工作流只读源码文本，不启动 Electron、不访问网络，因此可在无桌面栈的环境下先拦截结构回退。
 * 示例：`node file-preview-structure.test.cjs`
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const read = relative => fs.readFileSync(path.join(__dirname, relative), "utf8");

const index = read("index.html");
const shell = read("styles/shell.css");
const tokens = read("styles/tokens.css");
const views = read("styles/views.css");
const app = read("app.js");
const previewModule = read("file-preview.js");

// === 挂载点位于壳层，顺序为 导航 → 会话区 → 预览列 → 检查器 ===

const shellBody = index.slice(index.indexOf('class="app-shell"'), index.indexOf("</div>\n    <span id=\"zoomAnnouncement\""));
const order = ["app-navigation", "app-workspace", "filePreview", "appInspector"].map(marker => {
  const at = shellBody.indexOf(marker);
  assert.ok(at >= 0, `壳层缺少 ${marker}`);
  return at;
});
assert.deepEqual([...order].sort((a, b) => a - b), order, "四块区域的 DOM 顺序必须是导航 → 会话区 → 预览列 → 检查器");
assert.match(shellBody, /shell-resizer-preview/);

// 未打开文件时预览列必须自带 hidden，避免首帧闪出空列
assert.match(index, /<aside id="filePreview" class="file-preview" aria-label="文件预览" hidden>/);

// 预览模块必须在 app.js 之前加载（app.js 初始化即需要其 createShelf）
const moduleAt = index.indexOf("file-preview.js");
const appAt = index.indexOf("app.js?v=");
assert.ok(moduleAt >= 0 && appAt > moduleAt, "file-preview.js 必须先于 app.js 加载");

// === 四栏尺寸与覆盖断点 ===

assert.match(shell, /\.file-preview\s*\{[^}]*flex:\s*0 0 var\(--preview-width\)/s);
assert.match(shell, /\.shell-resizer\.shell-resizer-preview\s*\{\s*left:\s*calc\(100% - var\(--inspector-width\) - var\(--preview-width\)/s);
assert.match(shell, /@media \(max-width: 1180px\)[\s\S]*?\.file-preview\s*\{[^}]*position:\s*absolute/s);
assert.match(shell, /@media \(max-width: 1180px\)[\s\S]*?\.shell-resizer\.shell-resizer-preview\s*\{\s*display:\s*none/s);
// var() 不能用于媒体查询条件，断点必须是字面量
assert.doesNotMatch(shell, /@media[^{]*var\(/);
assert.match(tokens, /--file-preview-min-width:\s*\d+px/);
assert.match(tokens, /--file-preview-max-width:\s*\d+px/);

// === 宿主预览不依赖插件注册表 ===

assert.match(app, /const filePreview = window\.focusFilePreview/);
assert.doesNotMatch(app, /pluginViewForMaterial/, "预览路径不得再经插件注册表判定");
assert.doesNotMatch(app, /mountFilePanel|bindPanelResizer|panelResizer/);
// 可见性必须与视图同源派生：render() 在所有分支之前调用它，判定里含视图条件
assert.match(app, /function syncFilePreviewVisibility\(\)\s*\{[^}]*state\.view === "focus"/s);
assert.match(app, /function render\(\)\s*\{[\s\S]{0,240}syncFilePreviewVisibility\(\)/);
assert.match(app, /function openFilePreview\(/);
assert.match(app, /function resetFilePreviews\(/);
// 缓存按文件分别保存，且切换标签页不得清缓存
assert.match(app, /contentByFile: new Map\(\)/);
assert.doesNotMatch(app, /state\.filePreview\.loadedId/, "旧的单值缓存字段不得复活");
const activateAt = app.indexOf("if (action === \"activate-file-preview\") {");
const activateBranch = app.slice(activateAt, app.indexOf("}", activateAt));
assert.ok(activateBranch.startsWith("if (action === \"activate-file-preview\")"), "应能定位切换标签页分支");
assert.doesNotMatch(activateBranch, /forgetFilePreview\(|contentByFile\.delete/, "切换标签页不得清缓存");
// 关闭标签页必须丢弃该文件的缓存与地址
const closeAt = app.indexOf("} else {", activateAt);
const closeBranch = app.slice(closeAt, app.indexOf("renderFilePreview();", closeAt));
assert.match(closeBranch, /forgetFilePreview\(item\)/, "关闭标签页必须丢弃该文件的缓存");

// === 两个入口一律不得按扩展名设门 ===
// 历史缺陷：消息文件卡片已对所有文件可点，正文链接却仍被 FILE_VIEWABLE_RE 拦住，
// 导致 .json/.py/.svg 一类点击后毫无反应，违背「所有文件类型都有可用的呈现」。
assert.doesNotMatch(app, /FILE_VIEWABLE_RE/, "链接入口不得再按扩展名设门");
assert.doesNotMatch(app, /VIEWABLE_RE|VIEWABLE_PATTERN/);
// 链接入口在取得文件名后必须无条件交给预览，不再有 test() 判定
assert.match(app, /const fileName = fileNameFromLinkHref\(href, hostPart\)/);
assert.match(app, /if \(!fileName\) return;/);
// 链接入口必须交出**绝对路径**，而不是只留 basename —— 丢掉它会让按路径读取拿不到可用路径，
// 实测表现就是「仅接受绝对路径」并退化为信息卡。
assert.match(app, /function absolutePathFromHref\(href\)/);
assert.match(app, /const absolutePath = absolutePathFromHref\(href\)/);
assert.match(app, /relative_path: fileName, path: absolutePath \|\| fileName/);
// 相对路径由主进程结合工作区根解析，越界仍被拒绝
assert.match(app, /readPreviewBytes\(previewPathOf\(item\), activeTask\(\)\?\.workspace_path \|\| ""\)/);
assert.match(read("main.cjs"), /function resolvePreviewCandidate\(filePath, workspacePath\)/);
assert.match(read("main.cjs"), /relative\.startsWith\("\.\."\)/);
// host 不得再用自定义扩展名集合判定（classify 内部的后缀表是唯一来源）。
// 只在链接处理器区块内断言：renderFileCard 里选择图标的扩展名正则是纯装饰，不是门。
const linkHandler = app.slice(app.indexOf('document.addEventListener("click"'), app.indexOf('// 全局错误可见化'));
assert.ok(linkHandler.length > 0, "应能定位链接处理器区块");
assert.ok(
  linkHandler.includes('filePreview.classify(hostPart.replace(/\\/$/, "")) !== "binary"'),
  "外链与文件误解析的分流必须复用 classify，而不是自带后缀表"
);
assert.doesNotMatch(linkHandler, /\|jpe\?g\||\|docx\?\)/, "链接处理器不得自带扩展名集合");
// 未登记为材料也必须可点：卡片一律渲染为按钮，且不带「类型不支持」分支
assert.match(app, /function renderFileCard\(file, task\)/);
assert.match(app, /data-action="open-file-panel"\$\{materialId\}/);
assert.doesNotMatch(app, /file-card is-plain/, "未登记文件不得渲染为不可点占位");
// 卡片把消息项自带的绝对路径一并带出
assert.match(app, /data-file-path="\$\{escapeHtml\(file\.path\)\}"/);
// 材料匹配在精确等值未命中时按 basename 再匹配，避免丢掉 size_bytes 与真实路径
assert.match(app, /const exact = materials\.find\(item => item\.relative_path === fileName/);
assert.match(app, /basenameOf\(item\.relative_path\) === fileName/);
// 入口在没有可用路径时才拒绝，并给出可见说明而不是静默返回
assert.match(app, /function openFilePreview\(record\)\s*\{[\s\S]{0,400}setStatus\("无法定位该文件/);
// 失败原因必须可区分：415（不是文本）与 404（文件不存在）各自有说明
assert.match(app, /function previewFailureReason\(error, item\)/);
assert.match(app, /error\?\.status === 415[\s\S]{0,160}无法按文本呈现/);
assert.match(app, /error\?\.status === 404[\s\S]{0,160}文件不存在/);
// 降级原因必须进入正文，而不是只给沉默的卡片
assert.match(app, /kind: "binary", item, note: reason/);
assert.match(previewModule, /if \(view\.note\) appendText\(document, card, "p", "file-preview-empty", view\.note\)/);
// 卡片不再重复文件名第二遍
assert.doesNotMatch(previewModule, /file-preview-binary-name/);
// 渲染器不得 fetch 本地绝对路径：fetch("C:/…") 不是可解析 URL（表现为 Failed to fetch），
// 而 file:// 会让页面获得 Electron 安全指南劝阻的本机文件特权。字节一律经主进程读取。
const fetchSites = (app.match(/fetch\(/g) || []).length;
assert.equal(fetchSites, 2, `渲染器只应有 api() 与材料字节流两处 fetch，实际 ${fetchSites} 处`);
assert.doesNotMatch(app, /fetch\(resolved\.path\)/);
assert.match(app, /readPreviewBytes\(previewPathOf\(item\), activeTask\(\)\?\.workspace_path \|\| ""\)/);
assert.match(read("main.cjs"), /ipcMain\.handle\("focus:read-preview-bytes"/);
assert.match(read("main.cjs"), /fs\.promises\.readFile\(resolved\.path\)/);
assert.match(read("preload.cjs"), /readPreviewBytes: \(filePath, workspacePath\) => ipcRenderer\.invoke\("focus:read-preview-bytes"/);
// 后端已回报的截断与编码必须接入呈现，而不是被丢弃
assert.match(app, /truncated: Boolean\(payload\?\.truncated\)/);
assert.match(app, /encoding: payload\?\.encoding/);

// === 按路径读取只经主进程 IPC，不得暴露为 HTTP 路由 ===

const mainSrc = read("main.cjs");
const preloadSrc = read("preload.cjs");
assert.match(mainSrc, /function resolvePreviewPath\(filePath, workspacePath\)/);
assert.match(mainSrc, /ipcMain\.handle\("focus:resolve-preview-path"/);
assert.match(mainSrc, /此处有意不做工作区包含性检查|有意不做工作区包含性检查/);
assert.match(mainSrc, /应用数据目录不可预览/);
assert.match(preloadSrc, /resolvePreviewPath: \(filePath, workspacePath\) => ipcRenderer\.invoke\("focus:resolve-preview-path"/);
// 后端路由里不得出现按任意路径读取的入口
const routesSrc = read("../backend/app/desktop/routes.py");
assert.doesNotMatch(routesSrc, /preview-by-path|content-by-path|read_any_path/);

// === 预览模块的值域封闭与兜底 ===

assert.match(previewModule, /global\.focusFilePreview = \{/);
for (const exported of ["classify", "createShelf", "renderLineNumbers", "binaryCardModel", "mount"]) {
  assert.match(previewModule, new RegExp(`\\b${exported}\\b`), `预览模块必须对外提供 ${exported}`);
}
assert.match(previewModule, /return KIND_BY_SUFFIX\.get\(suffixOf\(name\)\) \|\| "binary"/);
// 未知类型走信息卡；图片缺少可用地址时同样落到信息卡，保证任何文件都有呈现
assert.match(previewModule, /current\.kind === "image" && !current\.url[\s\S]{0,120}renderBinaryCard/);
assert.match(previewModule, /RENDERERS\[current\.kind\] \|\| renderBinaryCard/);
// 正文不得再起「身份头部」：文件名已在标签页上，重复一遍就是对着标签打第二遍。
// 截断与编码改成挂在正文上的状态条，不再需要头部。
assert.doesNotMatch(previewModule, /renderHeader|file-preview-head|file-preview-name/);
assert.match(previewModule, /function appendStatusFlags\(parent, document, meta\)/);
assert.match(previewModule, /file-preview-flags/);
assert.match(read("styles/shell.css"), /\.file-preview-flags\b/);
assert.doesNotMatch(read("styles/shell.css"), /\.file-preview-name\b|\.file-preview-head\b/);
// 由字节构造的地址必须可回收
assert.match(previewModule, /function createObjectUrls\(urlApi\)[\s\S]{0,400}revokeObjectURL/);
// 按路径读取的文本由渲染器解码：主进程的 TextDecoder 不支持 gb18030
assert.match(previewModule, /DECODE_ENCODINGS = Object\.freeze\(\["utf-8", "gb18030"\]\)/);

// === 旧面板样式保留为插件契约，宿主自身不再引用 ===

assert.match(views, /宿主对插件公开的挂载契约/);
assert.match(views, /\.file-panel-head/);
assert.doesNotMatch(app, /class="file-panel/);

// === 信息卡的两个出路经受限 IPC，渲染器不接触文件系统 ===

const main = read("main.cjs");
const preload = read("preload.cjs");
assert.match(main, /ipcMain\.handle\("focus:open-path"/);
assert.match(main, /function containedMaterialPath\(/);
assert.match(main, /ipcMain\.handle\("focus:save-bytes"/);
assert.match(preload, /openPath: \(filePath, workspacePath\) => ipcRenderer\.invoke\("focus:open-path"/);
assert.match(preload, /saveBytes: \(suggestedName, bytes\) => ipcRenderer\.invoke\("focus:save-bytes"/);
// 信息卡的两个出路由预览模块标注，app.js 负责接线
assert.match(app, /action === "open-preview-in-system"/);
assert.match(app, /action === "download-preview-file"/);
assert.match(previewModule, /dataset\.action = "open-preview-in-system"/);
assert.match(previewModule, /dataset\.action = "download-preview-file"/);
// 渲染器不得直接写文件系统
assert.doesNotMatch(app, /require\("node:fs"\)|writeFileSync/);

console.log("file-preview-structure: all assertions passed");
