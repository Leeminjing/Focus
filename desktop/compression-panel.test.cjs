const test = require("node:test");
const assert = require("node:assert/strict");
const panel = require("./compression-panel.js");

const messages = [
  { id: "m1", role: "human", content: "讨论数据库方案" },
  { id: "a1", role: "ai", content: "", tool_calls: [{ id: "c1", name: "read_file", args: {} }] },
  { id: "t1", role: "tool", tool_call_id: "c1", name: "read_file", content: "文件内容" },
  { id: "m4", role: "ai", content: "最终决定使用 SQLite" },
  { id: "m5", role: "human", content: "确认离线运行" },
];

test("token 估算与后端同公式（CJK=1、其余÷4、每条 +12）", () => {
  assert.equal(panel.estimateRawTokens("你好", 0), 2 + 12 * 2);
  assert.equal(panel.estimateRawTokens("abcd", 0), Math.floor((4 + 3) / 4) + 12 * 2);
  assert.ok(panel.estimateMessagesTokens(messages) > 0);
});

test("toggleSelect 单条自由切换：不联动、不锁定", () => {
  let selected = new Set();
  selected = panel.toggleSelect(selected, 1);
  assert.deepEqual([...selected], [1]);
  selected = panel.toggleSelect(selected, 2);
  assert.deepEqual([...selected].sort(), [1, 2]);
  selected = panel.toggleSelect(selected, 1);
  assert.deepEqual([...selected], [2]);
});

test("selectionRanges 把连续选中段切为多个互不重叠的范围", () => {
  const selected = new Set([0, 1, 2, 4]);
  assert.deepEqual(panel.selectionRanges(selected), [
    { start: 0, end: 2 },
    { start: 4, end: 4 },
  ]);
});

test("planAfterMessages：压缩范围在首条来源位置替换为块，未选中消息原样", () => {
  const after = panel.planAfterMessages(messages, [
    { source_ids: ["m1", "a1", "t1"], replacement: "summary A" },
  ]);
  assert.deepEqual(after.map(m => m.content), ["summary A", "最终决定使用 SQLite", "确认离线运行"]);
});

test("planAfterMessages：多范围各自成块且边界保留", () => {
  const after = panel.planAfterMessages(messages, [
    { source_ids: ["m1", "a1"], replacement: "A" },
    { source_ids: ["m5"], replacement: "B" },
  ]);
  assert.deepEqual(after.map(m => m.content), ["A", "文件内容", "最终决定使用 SQLite", "B"]);
});

test("planAfterMessages：restore 原位展开压缩块来源原文", () => {
  const withBlock = [
    { id: "m1", role: "human", content: "前面" },
    {
      id: "b1", role: "human", content: "summary",
      compression: { block_id: "b1", source: [{ id: "x1", role: "human", content: "原文A" }, { id: "x2", role: "ai", content: "原文B" }] },
    },
    { id: "m5", role: "human", content: "后面" },
  ];
  const after = panel.planAfterMessages(withBlock, [{ source_ids: ["b1"], restore: true }]);
  assert.deepEqual(after.map(m => m.content), ["前面", "原文A", "原文B", "后面"]);
});

test("beforeAfter：统计与替换关系清单", () => {
  const stats = panel.beforeAfter(messages, [
    { source_ids: ["m1", "a1", "t1"], replacement: "summary A" },
    { source_ids: ["m5"], replacement: "summary B" },
  ]);
  assert.equal(stats.beforeCount, 5);
  assert.equal(stats.afterCount, 3);
  assert.ok(stats.afterTokens < stats.beforeTokens);
  assert.deepEqual(stats.mapping.map(item => item.label), ["消息 1~3", "消息 5~5"]);
});

test("expandForConversation 展开压缩块为原文并插入分界标记", () => {
  const flat = panel.expandForConversation([
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
  const flat = panel.expandForConversation([
    { id: "b1", role: "human", content: "外层摘要", compression: { block_id: "b1", source: [
      { id: "x1", role: "human", content: "原文A" },
      { id: "b2", role: "human", content: "内层摘要", compression: { block_id: "b2", source: [{ id: "y1", role: "ai", content: "原文B" }] } },
    ] } },
    { id: "s1", role: "tool", tool_call_id: "c1", name: "t", content: "占位", curation_synthetic: true },
  ]);
  assert.deepEqual(flat.map(item => (item.divider ? "D" : item.content)), ["D", "原文A", "D", "原文B"]);
  assert.equal(flat[2].depth, 1);
});

test("degradedParts 解析后端降级包装为工具结果", () => {
  const parts = panel.degradedParts({ role: "human", content: '<focus-degraded-message role="tool" name="list_files">\nC:\path\a.md\nC:\path\b.md\n</focus-degraded-message>' });
  assert.deepEqual(parts, { name: "list_files", content: "C:\path\a.md\nC:\path\b.md" });
  assert.equal(panel.degradedParts({ role: "human", content: "普通消息" }), null);
  assert.equal(panel.degradedParts({ role: "tool", name: "t", content: "普通工具结果" }), null);
});

test("planAfterMessages：delete 范围生成墓碑（来源保留、模型不可见）", () => {
  const after = panel.planAfterMessages(messages, [
    { source_ids: ["a1", "t1"], delete: true },
  ]);
  assert.deepEqual(after.map(m => m.content), ["讨论数据库方案", "", "最终决定使用 SQLite", "确认离线运行"]);
  assert.equal(after[1].compression.deleted, true);
});

test("beforeAfter：delete 映射标记为删除", () => {
  const stats = panel.beforeAfter(messages, [{ source_ids: ["m5"], delete: true }]);
  assert.equal(stats.afterCount, 5);
  assert.deepEqual(stats.mapping[0], { label: "消息 5~5", sourceIds: ["m5"], restore: false, delete: true });
});

test("expandForConversation：删除墓碑展开原文并标记已删除", () => {
  const flat = panel.expandForConversation([
    { id: "m1", role: "human", content: "开始" },
    { id: "t1", role: "human", content: "", compression: { block_id: "t1", deleted: true, source: [
      { id: "x1", role: "human", content: "被删除的消息" },
    ] } },
    { id: "m5", role: "ai", content: "结束" },
  ]);
  assert.deepEqual(flat.map(item => (item.divider ? "D" : item.content)), ["开始", "D", "被删除的消息", "结束"]);
  assert.equal(flat[1].deleted, true);
});
