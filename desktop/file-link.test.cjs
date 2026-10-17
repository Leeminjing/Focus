const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readAppSource } = require("./test-helper.cjs");

const functionSource = readAppSource().match(/function filePathFromLinkHref[\s\S]*?\n}/)?.[0];
assert.ok(functionSource, "应能从 app.js 加载文件链接解析函数");
const context = vm.createContext({ decodeURIComponent, URL });
new vm.Script(functionSource).runInContext(context);

const filePathFromHref = href => new vm.Script(
  `filePathFromLinkHref(${JSON.stringify(href)})`,
).runInContext(context);

assert.equal(
  filePathFromHref("file:///C:/Users/brubing/Desktop/newthree/%E5%B0%8F%E8%8A%B1%E4%BB%99.docx"),
  "C:/Users/brubing/Desktop/newthree/小花仙.docx",
  "file URL 的完整路径只来自 href，不受带图标的显示文字影响",
);

const pathFunction = readAppSource().match(/function filePathFromLinkHref[\s\S]*?\n}/)?.[0];
new vm.Script(pathFunction).runInContext(context);
const filePath = href => new vm.Script(`filePathFromLinkHref(${JSON.stringify(href)})`).runInContext(context);
assert.equal(filePath("file:///C:/workspace/reports/%E4%BA%A7%E7%89%A9.csv"), "C:/workspace/reports/产物.csv");
assert.equal(filePath("reports/nested/data.json#L10"), "reports/nested/data.json");
assert.equal(filePath("file:///home/user/reports/data.json"), "/home/user/reports/data.json");
assert.equal(
  filePathFromHref("http://%E6%96%87%E4%BB%B6%E5%88%97%E8%A1%A8.md/"),
  null,
  "不能根据 HTTP host 猜测本地文件路径",
);

console.log("file-link.test.cjs OK");
