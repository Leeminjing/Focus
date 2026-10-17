const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readAppSource } = require("./test-helper.cjs");

const functionSource = readAppSource().match(/function filePathFromLinkHref[\s\S]*?\n}/)?.[0];
assert.ok(functionSource, "应能从 app.js 加载文件链接解析函数");
const context = vm.createContext({ decodeURIComponent });
new vm.Script(functionSource).runInContext(context);

const filePathFromHref = href => new vm.Script(
  `filePathFromLinkHref(${JSON.stringify(href)})`,
).runInContext(context);

assert.equal(
  filePathFromHref("file:///C:/Users/brubing/Desktop/newthree/%E5%B0%8F%E8%8A%B1%E4%BB%99.docx"),
  "C:/Users/brubing/Desktop/newthree/小花仙.docx",
  "file URL 的完整路径只来自 href，不受带图标的显示文字影响",
);
assert.equal(
  filePathFromHref("http://%E6%96%87%E4%BB%B6%E5%88%97%E8%A1%A8.md/"),
  null,
  "不能根据 HTTP host 猜测本地文件路径",
);

console.log("file-link.test.cjs OK");
