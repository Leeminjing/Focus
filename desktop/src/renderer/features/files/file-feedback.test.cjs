/* 本文件验证文件版本引用的纯渲染与草稿冻结。
 * 输入为 V1/V2 文件事实和失败回执；输出为精确版本保留、转义和不可用 Diff 断言。
 * 工作流在普通输入中引用 V1、提交后编辑 V2，再重试 V1；示例：node --test desktop/src/renderer/features/files/file-feedback.test.cjs。
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const inputs = require("../patrol/workspace/workspace-patrol-inputs.js");
const facts = require("../loop/loop-facts-view.js");

test("file references are frozen with ordinary input and cleared only from sent draft", () => {
  const store = inputs.create(() => "input");
  const v1 = { fact_id: "one", revision: 2, result_key: "a".repeat(64), slot_id: "slot", path: "plan.md", sha256: "b".repeat(64) };
  store.edit({ content: "不满意，请先诊断", file_refs: [v1] });
  const sent = store.submit();
  v1.revision = 99;
  assert.equal(sent.request.input_type, "information");
  assert.equal(sent.request.file_refs[0].revision, 2);
  assert.deepEqual(store.get().draft.file_refs, []);
  store.edit({ content: "下一条输入", file_refs: [{ ...v1, result_key: "c".repeat(64) }] });
  store.failed("input", new Error("断线"));
  assert.equal(store.retry("input").request, sent.request);
  assert.equal(store.get().draft.content, "下一条输入");
});

test("file detail escapes snapshots and only renders Diff with both observed versions", () => {
  const file = { path: "<plan>.md", sha256: "a".repeat(64), before: { available: false, reason: "before_unobserved" }, after: { available: true, text: "<script>" } };
  const missing = facts.renderFile(file);
  assert.match(missing, /无法生成可靠 Diff/);
  assert.doesNotMatch(missing, /class="file-diff"/);
  assert.match(missing, /&lt;script&gt;/);
  const complete = facts.renderFile({ ...file, before: { available: true, text: "old" } });
  assert.match(complete, /class="removed">−old/);
  assert.match(complete, /class="added">\+&lt;script&gt;/);
});

test("importance follows explicit file identity without changing verification or row density", () => {
  const row = { fact_id: "f", revision: 1, status: "observed", file: {slot_id:"one",path:"long/".repeat(40)+"plan.md",sha256:"a".repeat(64),operation:"modified"} };
  assert.doesNotMatch(facts.renderTimeline([row]), /is-important/);
  const selected = facts.markFileReferences([row], [row.file]);
  const html = facts.renderTimeline(selected);
  assert.match(html, /is-important is-observed/);
  assert.match(html, /用户引用/);
  assert.match(html, /title="long\//);
  assert.equal(facts.markFileReferences([row], [{...row.file,slot_id:"other"}])[0],row);
  assert.match(facts.renderTimeline([{...row,file:{...row.file,importance:"declared_artifact"}}]), /关键产出/);
});

test("invalid file references preserve editable draft without overwriting later input", () => {
  let id = 0;
  const store = inputs.create(() => String(++id));
  const file = { result_key: "a", path: "plan.md" };
  store.edit({content:"纠正",file_refs:[file]});
  store.submit();
  store.failed("1",Object.assign(new Error("版本已失效"),{status:409}));
  assert.equal(store.get().draft.content,"纠正");
  store.edit({file_refs:[]});
  assert.deepEqual(store.submit().request.file_refs,[]);
  store.edit({content:"再次纠正",file_refs:[file]});
  store.submit();
  store.edit({content:"新的草稿"});
  store.failed("3",Object.assign(new Error("无权引用"),{status:403}));
  assert.equal(store.get().draft.content,"新的草稿");
  store.editFailed("3");
  assert.equal(store.get().draft.content,"新的草稿");
  store.edit({content:"",file_refs:[]});
  store.editFailed("3");
  assert.equal(store.get().draft.content,"再次纠正");
  assert.deepEqual(store.get().draft.file_refs,[file]);
});
