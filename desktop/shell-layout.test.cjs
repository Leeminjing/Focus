/*
 * 本文件验证壳层四栏宽度分配的钳制规则与覆盖断点。输入为布局草稿、视口宽度与壳层样式，
 * 输出为归一化后的四栏宽度、工作区保底是否被守住、边界内外行为以及覆盖层形态的断言；
 * 工作流在 VM 内执行 app.js 的真实实现并读取样式文本，不访问网络。
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

const readShellCss = () => fs.readFileSync(path.join(__dirname, "styles", "shell.css"), "utf8");

const { context } = createAppHarness();
new vm.Script(readAppSource()).runInContext(context);

const normalize = (layout, width) =>
  JSON.parse(JSON.stringify(vm.runInContext(`normalizeShellLayout(${JSON.stringify(layout)}, ${width})`, context)));

const bounds = JSON.parse(JSON.stringify(vm.runInContext("SHELL_LAYOUT_BOUNDS", context)));
const sideTotal = layout => layout.navWidth + layout.previewWidth + layout.inspectorWidth;
const workspaceWidth = (layout, viewport) => viewport - sideTotal(layout);

// === 默认值来自视口比例，且各自落在边界内 ===

const wide = normalize({}, 1600);
assert.ok(wide.previewWidth >= bounds.previewMin && wide.previewWidth <= bounds.previewMax);
assert.ok(wide.inspectorWidth >= bounds.inspectorMin && wide.inspectorWidth <= bounds.inspectorMax);
assert.equal(wide.customized, false);

// === 每栏都在自己的边界内被钳制 ===

assert.equal(normalize({ navWidth: 9999, previewWidth: 400, inspectorWidth: 400 }, 1600).navWidth, bounds.navMax);
assert.equal(normalize({ navWidth: 172, previewWidth: 1, inspectorWidth: 400 }, 1600).previewWidth, bounds.previewMin);
assert.equal(normalize({ navWidth: 172, previewWidth: 400, inspectorWidth: 1 }, 1600).inspectorWidth, bounds.inspectorMin);

// 视口足够宽时三栏都能到各自最大值；不足时按比例让位给工作区保底
const huge = normalize({ navWidth: 9999, previewWidth: 9999, inspectorWidth: 9999 }, 3000);
assert.equal(huge.navWidth, bounds.navMax);
assert.equal(huge.previewWidth, bounds.previewMax);
assert.equal(huge.inspectorWidth, bounds.inspectorMax);
assert.ok(workspaceWidth(huge, 3000) >= bounds.workspaceMin);

const squeezed = normalize({ navWidth: 9999, previewWidth: 9999, inspectorWidth: 9999 }, 1600);
assert.ok(squeezed.previewWidth < bounds.previewMax, "预算不足时预览列不应仍取最大值");
assert.equal(workspaceWidth(squeezed, 1600), bounds.workspaceMin);

// === 折叠态保留 navWidth 原值（由 applyShellLayout 在写入内联值时覆盖为 0，便于展开恢复） ===

const collapsed = normalize({ navWidth: 200, previewWidth: 400, inspectorWidth: 300, navCollapsed: true }, 1600);
assert.equal(collapsed.navCollapsed, true);
assert.equal(collapsed.navWidth, 200);

// === 宽屏下四栏并存，工作区拿到充足剩余宽度 ===

const roomy = normalize({ navWidth: 172, previewWidth: 400, inspectorWidth: 300 }, 1600);
assert.ok(workspaceWidth(roomy, 1600) >= bounds.workspaceMin);

// === 窄视口下为工作区保留最小宽度 ===

for (const viewport of [1200, 1100, 1000, 900, 800, 640]) {
  const tight = normalize({ navWidth: 288, previewWidth: 720, inspectorWidth: 480 }, viewport);
  const workspace = workspaceWidth(tight, viewport);
  assert.ok(
    workspace >= bounds.workspaceMin,
    `视口 ${viewport} 下工作区被挤穿：${workspace}`
  );
  assert.ok(tight.navWidth <= bounds.navMax && tight.previewWidth <= bounds.previewMax && tight.inspectorWidth <= bounds.inspectorMax);
}

// === 覆盖断点以下：预览列不参与宽度分配，因此不会被压没 ===
// 1180px 及以下会话页预览列转为绝对定位覆盖层（shell.css），此时 --preview-width
// 无论被钳制到多少都不影响呈现宽度，故不存在「列在而内容不可见」。

const OVERLAY_BREAKPOINT = 1180;
assert.ok(OVERLAY_BREAKPOINT > bounds.workspaceMin, "覆盖断点必须高于工作区保底，否则四栏会先互相挤压");
const overlayShell = readShellCss();
assert.match(overlayShell, /@media \(max-width: 1180px\)[\s\S]*?\.file-preview\s*\{[^}]*position:\s*absolute/s);
assert.match(overlayShell, /@media \(max-width: 1180px\)[\s\S]*?\.file-preview\s*\{[^}]*width:\s*min\(/s);
// 覆盖层形态下预览列不参与 flex 分配，故其最小宽度约束不适用于断点以下的视口
assert.match(overlayShell, /@media \(max-width: 1180px\)[\s\S]*?\.file-preview\s*\{[^}]*flex:\s*none/s);

// === 极窄视口：即便四栏最小值之和超过视口，也不出现负宽度 ===

const extreme = normalize({ navWidth: 288, previewWidth: 720, inspectorWidth: 480 }, 400);
for (const [key, value] of Object.entries(extreme)) {
  if (key === "navCollapsed" || key === "customized") continue;
  assert.ok(value >= 0, `${key} 为负：${value}`);
}

// === 非数值输入一律回落，不产生 NaN 宽度 ===

const garbage = normalize({ navWidth: "abc", previewWidth: null, inspectorWidth: undefined }, 1280);
for (const key of ["navWidth", "previewWidth", "inspectorWidth"]) {
  assert.ok(Number.isFinite(garbage[key]), `${key} 不是有限数：${garbage[key]}`);
}
assert.equal(normalize(null, 1280).previewWidth >= bounds.previewMin, true);

// === 归一化保持幂等：对结果再归一化不再变化 ===

const once = normalize({ navWidth: 250, previewWidth: 600, inspectorWidth: 420 }, 1000);
const twice = normalize(once, 1000);
assert.deepEqual(twice, once);

console.log("shell-layout: all assertions passed");
