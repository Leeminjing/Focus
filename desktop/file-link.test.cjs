/*
 * 本文件验证消息链接的路径解析：输入为消息里的 href 与显示文字，输出为文件名与磁盘绝对路径。
 * 绝对路径这一项是回归守卫——入口曾只取 basename 而丢掉绝对路径，导致按路径预览报
 * 「仅接受绝对路径」并退化成一张没有内容的卡片。
 */
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readAppSource } = require("./test-helper.cjs");

const appSource = readAppSource();
const functionSource = appSource.match(/function fileNameFromLinkHref[\s\S]*?\n}/)?.[0];
assert.ok(functionSource, "应能从 app.js 加载文件链接解析函数");
const pathSource = appSource.match(/function absolutePathFromHref[\s\S]*?\n}/)?.[0];
assert.ok(pathSource, "应能从 app.js 加载绝对路径还原函数");

const context = vm.createContext({ decodeURIComponent });
new vm.Script(`${functionSource}\n${pathSource}`).runInContext(context);

const fileNameFromHref = (href, hostPart = "") => new vm.Script(
  `fileNameFromLinkHref(${JSON.stringify(href)}, ${JSON.stringify(hostPart)})`,
).runInContext(context);
const absolutePathFromHref = href => new vm.Script(
  `absolutePathFromHref(${JSON.stringify(href)})`,
).runInContext(context);

assert.equal(
  fileNameFromHref("file:///C:/Users/brubing/Desktop/newthree/%E5%B0%8F%E8%8A%B1%E4%BB%99.docx"),
  "小花仙.docx",
  "file URL 的文件名只来自 href，不受带图标的显示文字影响",
);
assert.equal(
  fileNameFromHref("http://%E6%96%87%E4%BB%B6%E5%88%97%E8%A1%A8.md/", "%E6%96%87%E4%BB%B6%E5%88%97%E8%A1%A8.md"),
  "文件列表.md",
  "保留 markdown-it 误解析中文文件链接的兼容分支",
);

// === 绝对路径还原：file:///C:/... 必须还原成 C:\... 形态的绝对路径 ===

assert.equal(
  absolutePathFromHref("file:///C:/Users/brubing/Desktop/newthree/%E5%B0%8F%E8%8A%B1%E4%BB%99.docx"),
  "C:/Users/brubing/Desktop/newthree/小花仙.docx",
  "去掉协议与前导斜杠后应得到 Windows 绝对路径，并解码百分号编码",
);
assert.equal(
  absolutePathFromHref("file:///C:/workspace/%E6%96%87%E4%BB%B6%E5%88%97%E8%A1%A8.md"),
  "C:/workspace/文件列表.md",
);
assert.equal(
  absolutePathFromHref("file:///home/user/notes.md"),
  "/home/user/notes.md",
  "POSIX 形态保留前导斜杠",
);
assert.equal(
  absolutePathFromHref("file:///C:/a/b.md?rev=2#top"),
  "C:/a/b.md",
  "查询串与锚点不进入路径",
);
assert.equal(absolutePathFromHref("https://example.com/a.md"), "", "非 file: 链接没有可还原的绝对路径");
assert.equal(absolutePathFromHref("file:///C:/a/%ZZ.md"), "C:/a/%ZZ.md", "解码失败时保留原串交给主进程判定存在性");

console.log("file-link.test.cjs OK");
