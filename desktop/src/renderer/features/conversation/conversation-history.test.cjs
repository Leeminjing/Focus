const test = require("node:test");
const assert = require("node:assert/strict");
const history = require("./conversation-history.js");

test("expandForConversation 展开压缩块为原文并插入分界标记", () => {
  const flat = history.expandForConversation([
    { id: "m1", role: "human", content: "开始" },
    { id: "b1", role: "human", content: "摘要", compression: { block_id: "b1", source: [
      { id: "x1", role: "human", content: "原文A" },
      { id: "x2", role: "ai", content: "原文B" },
    ] } },
    { id: "m5", role: "ai", content: "结束" },
  ]);
  assert.deepEqual(flat.map(item => (item.divider ? "D" : item.content)), ["开始", "D", "原文A", "原文B", "结束"]);
  assert.equal(flat[1].summary, "摘要");
  assert.equal(flat[1].count, 2);
});

test("expandForConversation 递归展开嵌套块并跳过 curation_synthetic", () => {
  const flat = history.expandForConversation([
    { id: "b1", role: "human", content: "外层摘要", compression: { block_id: "b1", source: [
      { id: "x1", role: "human", content: "原文A" },
      { id: "b2", role: "human", content: "内层摘要", compression: { block_id: "b2", source: [{ id: "y1", role: "ai", content: "原文B" }] } },
    ] } },
    { id: "s1", role: "tool", tool_call_id: "c1", name: "t", content: "占位", curation_synthetic: true },
  ]);
  assert.deepEqual(flat.map(item => (item.divider ? "D" : item.content)), ["D", "原文A", "D", "原文B"]);
  assert.equal(flat[2].depth, 1);
});

test("expandForConversation 保留策展总结，只隐藏协议占位", () => {
  const flat = history.expandForConversation([
    {
      id: "legacy-summary",
      role: "ai",
      content: "旧版策展总结",
      curation_synthetic: true,
      curation_source_message_ids: ["source-1"],
    },
    {
      id: "summary",
      role: "ai",
      content: "新版策展总结",
      curation_source_message_ids: ["source-2"],
    },
    {
      id: "placeholder",
      role: "tool",
      tool_call_id: "call-1",
      name: "read_file",
      content: "[Focus placeholder: tool result omitted]",
      curation_synthetic: true,
    },
  ]);

  assert.deepEqual(flat.map(item => item.content), ["旧版策展总结", "新版策展总结"]);
});

test("degradedParts 解析后端降级包装为工具结果", () => {
  // Historical tool results remain readable after the file discovery tools merge.
  const parts = history.degradedParts({ role: "human", content: '<focus-degraded-message role="tool" name="list_files">\nC:\path\a.md\nC:\path\b.md\n</focus-degraded-message>' });
  assert.deepEqual(parts, { name: "list_files", content: "C:\path\a.md\nC:\path\b.md" });
  assert.equal(history.degradedParts({ role: "human", content: "普通消息" }), null);
  assert.equal(history.degradedParts({ role: "tool", name: "t", content: "普通工具结果" }), null);
});

test("expandForConversation：删除墓碑展开原文并标记已删除", () => {
  const flat = history.expandForConversation([
    { id: "m1", role: "human", content: "开始" },
    { id: "t1", role: "human", content: "", compression: { block_id: "t1", deleted: true, source: [
      { id: "x1", role: "human", content: "被删除的消息" },
    ] } },
    { id: "m5", role: "ai", content: "结束" },
  ]);
  assert.deepEqual(flat.map(item => (item.divider ? "D" : item.content)), ["开始", "D", "被删除的消息", "结束"]);
  assert.equal(flat[1].deleted, true);
});
