// md 渲染测试:renderAssistantContent 经 markdown-it 渲染完整语法 + XSS 防护
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

const { context } = createAppHarness();
new vm.Script(readAppSource()).runInContext(context);

const render = content =>
  new vm.Script(`renderAssistantContent(${JSON.stringify(content)})`).runInContext(context);

// 标题 / 加粗 / 行内代码
let html = render("## 标题\n\n**加粗** `code`");
assert.match(html, /<h2>标题<\/h2>/, "## 标题渲染为 h2");
assert.match(html, /<strong>加粗<\/strong>/, "**加粗** 渲染为 strong");
assert.match(html, /<code>code<\/code>/, "`code` 渲染为行内 code");

// 无序 / 有序列表
html = render("- a\n- b\n\n1. x\n2. y");
assert.match(html, /<ul>\s*<li>a<\/li>/, "无序列表渲染");
assert.match(html, /<ol>\s*<li>x<\/li>/, "有序列表渲染");

// 代码块
html = render("```js\nconsole.log(1)\n```");
assert.match(html, /<pre><code class="language-js">console\.log\(1\)/, "围栏代码块渲染");

// 引用 / 表格
html = render("> quote\n\n|a|b|\n|-|-|\n|1|2|");
assert.match(html, /<blockquote>/, "引用渲染");
assert.match(html, /<table>/, "表格渲染");

// 链接
html = render("[链接](https://example.com)");
assert.match(html, /<a href="https:\/\/example\.com">链接<\/a>/, "普通链接渲染");

// XSS:原始 HTML 转义不执行
html = render("<script>alert(1)</script>");
assert.ok(!html.includes("<script>alert"), "原始 script 不渲染");
assert.ok(html.includes("&lt;script&gt;"), "script 标签转义显示");

// 危险协议链接拒绝(validateLink):不生成 <a href="javascript:...">,源文本原样保留
html = render("[x](javascript:alert(1))");
assert.ok(!html.includes('<a href="javascript:'), "javascript: 链接不生成");
assert.match(html, /\[x\]\(javascript:alert\(1\)\)/, "危险链接渲染为纯文本");

console.log("md-render.test.cjs OK");
