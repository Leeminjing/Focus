/* 验证 reasoning/tool 消息归一为紧凑、无重复且安全的会话事件。 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const events = require("./conversation-events.js");

const messages = [
  {
    role: "ai",
    content: "",
    reasoning_content: "先检查用户给出的路径，再读取工作区文件。",
    tool_calls: [
      { id: "call-read", name: "read_file", args: { path: "docs/very/long/path/to/requirements.md" } },
      { id: "call-list", name: "list_files", args: { path: "docs" } },
    ],
  },
  {
    role: "tool",
    name: "read_file",
    tool_call_id: "call-read",
    status: "error",
    content: "路径不属于当前工作区: <outside>",
  },
];

const normalized = events.normalize(messages);
assert.deepEqual(normalized.map(item => item.type), ["reasoning", "tool", "tool"]);
assert.equal(normalized.filter(item => item.type === "tool").length, 2, "每个 call 只能生成一条逻辑工具事件");
assert.equal(normalized[1].status, "pending");
assert.equal(normalized[2].status, "error");
assert.equal(normalized[2].call.id, "call-read");

const reasoningHtml = events.renderEvent(normalized[0]);
assert.match(reasoningHtml, /<details/);
assert.match(reasoningHtml, /Think/);
assert.match(reasoningHtml, /先检查用户给出的路径/);

const longReasoning = `最早的思考不会永远占据实时预览。${"中间推理".repeat(30)}最新进展<&>`;
assert.equal(
  events.preview(longReasoning, 24),
  `${longReasoning.replace(/\s+/g, " ").trim().slice(0, 23)}…`,
  "完成态默认保留稳定的开头摘要",
);
assert.equal(
  events.preview(longReasoning, 24, "latest"),
  `…${longReasoning.replace(/\s+/g, " ").trim().slice(-23)}`,
  "活动流预览必须包含最新 reasoning",
);
assert.equal(events.preview("  短内容\n继续  ", 24, "latest"), "短内容 继续", "未超限时显示完整规范化内容");
assert.equal(events.preview("已有内容   \n", 24, "latest"), "已有内容", "尾部空白不应制造空预览");

const liveReasoningHtml = events.renderEvent(
  { type: "reasoning", content: longReasoning },
  { previewMode: "latest" },
);
const liveSummary = liveReasoningHtml.match(/<summary>([\s\S]*?)<\/summary>/)?.[1] || "";
assert.match(liveSummary, /最新进展&lt;&amp;&gt;/, "活动流 summary 显示最新片段并安全转义");
assert.doesNotMatch(liveSummary, /最早的思考/, "活动流 summary 不固定在最早内容");
assert.match(liveReasoningHtml, /最早的思考/, "展开详情保留最早内容");
assert.match(liveReasoningHtml, /最新进展&lt;&amp;&gt;/, "展开详情保留最新内容并安全转义");

const errorHtml = events.renderEvent(normalized[2]);
assert.match(errorHtml, /失败/);
assert.match(errorHtml, /路径不属于当前工作区/);
assert.doesNotMatch(errorHtml, /<outside>/);
assert.match(errorHtml, /&lt;outside&gt;/);
assert.match(errorHtml, /<pre/);

const eventStyles = fs.readFileSync(path.join(__dirname, "styles", "conversation-events.css"), "utf8");
const previewRule = eventStyles.match(/\.conversation-event-preview,\s*\n\.conversation-event-error\s*\{([\s\S]*?)\}/)?.[1] || "";
assert.match(previewRule, /min-width:\s*0/, "网格中的预览允许缩到可用宽度");
assert.match(previewRule, /overflow:\s*hidden/, "预览隐藏横向溢出");
assert.match(previewRule, /text-overflow:\s*ellipsis/, "预览使用省略号表达截断");
assert.match(previewRule, /white-space:\s*nowrap/, "折叠预览保持单行");

console.log("conversation-events: reasoning、pending、失败结果、无重复与安全展开通过");
