const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readAppSource } = require("./test-helper.cjs");

const functionSource = readAppSource().match(/function fileNameFromLinkHref[\s\S]*?\n}/)?.[0];
assert.ok(functionSource, "应能从 app.js 加载文件链接解析函数");
const context = vm.createContext({ decodeURIComponent });
new vm.Script(functionSource).runInContext(context);

const fileNameFromHref = (href, hostPart = "") => new vm.Script(
  `fileNameFromLinkHref(${JSON.stringify(href)}, ${JSON.stringify(hostPart)})`,
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

console.log("file-link.test.cjs OK");
