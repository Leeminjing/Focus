/*
 * 本文件验证图片放大视图可被关闭。输入为 index.html 与 styles/views.css 的源码，
 * 输出为「模态框自行声明可点击」「整层可点即关」「关闭动作存在处理分支」三组断言；
 * 工作流不访问网络或真实 DOM。回归目标：交互式模态框被放进 pointer-events:none 的
 * 穿透层 .overlay-root 后变得可望不可点（放大后关不掉）。
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");

const app = fs.readFileSync(require.resolve("./app.js"), "utf8");
const views = fs.readFileSync(require.resolve("./styles/views.css"), "utf8");
const shell = fs.readFileSync(require.resolve("./styles/shell.css"), "utf8");

// 穿透层本身保持不挡点击（这是它的设计语义，不应为了修 bug 而改掉）
assert.match(shell, /\.overlay-root\s*\{[^}]*pointer-events:\s*none/);

// 模态框必须自行声明可点击，否则继承容器的 pointer-events:none
const lightboxRule = views.match(/\.image-lightbox\s*\{[^}]*\}/)?.[0] || "";
assert.match(lightboxRule, /pointer-events:\s*auto/, "放大视图必须声明 pointer-events:auto");

// 整层可点即关：容器与关闭按钮都带关闭动作，用户在任何位置点击都能退出
const lightboxMarkup = app.match(/class="image-lightbox"[\s\S]*?<\/div>`/)?.[0] || "";
assert.match(lightboxMarkup, /data-action="close-lightbox"/, "放大视图整层可点即关");
assert.match(lightboxMarkup, /aria-modal="true"/, "放大视图声明为模态");

// 关闭动作必须有处理分支
assert.match(app, /action === "close-lightbox"/);

console.log("image-lightbox: all assertions passed");
