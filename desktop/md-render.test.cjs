/*
 * 本文件验证主任务 AI Markdown 与统一工作记录骨架。输入为消息对象和 Markdown 文本，输出为
 * 安全 HTML、折叠技术元数据及 Human/AI/Tool 语义变体断言；工作流不访问网络或真实 DOM。
 */
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

const renderMessage = message =>
  new vm.Script(`renderMessage(${JSON.stringify(message)})`).runInContext(context);

html = renderMessage({ id: "message-1", role: "ai", content: "**完成**", tool_calls: [{ id: "call-1", name: "search", args: { q: "x" } }] });
assert.match(html, /class="work-record message ai"/, "AI 使用统一工作记录骨架");
assert.match(html, /<strong>完成<\/strong>/, "工作记录继续使用安全 Markdown");
assert.match(html, /<details class="message-details">/, "技术字段默认进入折叠详情");
assert.match(html, /message-1/, "折叠详情保留完整消息 ID");
assert.match(html, /call-1/, "折叠详情保留完整工具调用载荷");

html = renderMessage({ role: "human", content: "<img src=x onerror=alert(1)>" });
assert.match(html, /class="work-record message human"/, "Human 使用统一工作记录骨架");
assert.ok(!html.includes("<img src=x"), "Human 工作记录仍转义原始 HTML");

html = renderMessage({ role: "tool", name: "powershell", tool_call_id: "call-tool", content: "done" });
assert.match(html, /class="work-record message tool"/, "Tool 使用统一工作记录骨架");
assert.match(html, /工具调用 ID/, "Tool ID 进入技术详情");

console.log("md-render.test.cjs OK");
