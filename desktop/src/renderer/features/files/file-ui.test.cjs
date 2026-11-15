/*
 * 本文件验证材料与文件工作台结构。输入为宿主渲染源码和文件查看器源码，
 * 输出为策略/版本入口、统一载体工具栏和文件面板骨架断言。
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const app = fs.readFileSync(path.join(__dirname, "../../app/app.js"), "utf8");
const viewer = fs.readFileSync(path.join(__dirname, "../../../../../plugins/spatial-patrol/desktop/viewer.js"), "utf8");
const views = fs.readFileSync(path.join(__dirname, "../../shared/styles/views.css"), "utf8");

assert.match(app, /TASK SOURCES/);
assert.match(app, /版本保护/);
assert.match(app, /检测到外部删除/);
assert.match(app, /data-action="load-versions"/);
assert.match(app, /data-action="restore-version"/);

assert.match(viewer, /FILE WORKBENCH/);
assert.match(viewer, /FocusFilePreview\.mount/);
assert.match(views, /宿主文件工作台骨架不依赖任何插件样式/);
assert.match(views, /\.file-panel-head/);

