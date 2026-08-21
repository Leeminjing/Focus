/*
 * 本文件验证 F20 材料与文件工作台结构。输入为宿主渲染源码和 spatial-patrol 查看器源码，
 * 输出为策略/版本入口、统一载体工具栏、DOCX 邻近授权、模式重置和锚点反馈断言。
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const app = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
const viewer = fs.readFileSync(path.join(__dirname, "..", "plugins", "spatial-patrol", "desktop", "viewer.js"), "utf8");
const views = fs.readFileSync(path.join(__dirname, "styles", "views.css"), "utf8");

assert.match(app, /TASK SOURCES/);
assert.match(app, /版本保护/);
assert.match(app, /检测到外部删除/);
assert.match(app, /data-action="load-versions"/);
assert.match(app, /data-action="restore-version"/);

assert.match(viewer, /FILE WORKBENCH · \$\{typeLabel\}/);
assert.match(viewer, /data-carrier=/);
assert.match(viewer, /本次操作权限（必选）/);
assert.match(viewer, /只读观察/);
assert.match(viewer, /修改文档/);
assert.ok((viewer.match(/state\.docxRunMode = null/g) || []).length >= 3, "打开新文件、关闭和切换内容必须清空 DOCX 模式");
assert.match(viewer, /text-character-v1/);
assert.match(viewer, /旧 DOCX 锚点不具备字符坐标语义/);
assert.match(viewer, /需要处理/);
assert.match(viewer, /锚点失效/);
assert.match(views, /宿主文件工作台骨架不依赖任何插件样式/);
assert.match(views, /\.file-panel-head/);

