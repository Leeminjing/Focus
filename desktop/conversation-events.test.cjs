/* 验证 reasoning/tool 消息归一为紧凑、无重复且安全的会话事件。 */
"use strict";

const assert = require("node:assert/strict");
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

const errorHtml = events.renderEvent(normalized[2]);
assert.match(errorHtml, /失败/);
assert.match(errorHtml, /路径不属于当前工作区/);
assert.doesNotMatch(errorHtml, /<outside>/);
assert.match(errorHtml, /&lt;outside&gt;/);
assert.match(errorHtml, /<pre/);

console.log("conversation-events: reasoning、pending、失败结果、无重复与安全展开通过");
