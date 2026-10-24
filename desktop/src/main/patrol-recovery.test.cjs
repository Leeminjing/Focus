/* 本文件验证真实磁盘仓库与 Patrol 输入编排的恢复边界。
 * 输入为临时 userData、重建的 intake、分页历史和中断的上传/回执；输出为字节/身份/请求保持与先对账断言。
 * 工作流覆盖刷新、跨端口重启、部分上传、写盘失败及安全清理；示例：node --test desktop/src/main/patrol-recovery.test.cjs。
 */
"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { PatrolRecovery } = require("./patrol-recovery.cjs");
const Intake = require("../renderer/features/patrol/workspace/workspace-patrol-intake.js");
const limits = { general_file_bytes: 1024, original_image_bytes: 512, run_material_count: 4 };
const text = value => ({ kind: "text", text: value });
const emptyHistory = () => Promise.resolve({ items: [], next_before: null });
function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "focus-patrol-recovery-"));
  let serial = 0;
  const uploads = [], posts = [];
  const repository = () => new PatrolRecovery(directory, limits.general_file_bytes);
  const create = ({ api = {}, ...options } = {}) => Intake.create({
    runtime: { apiBase: "http://127.0.0.1:1234", session: "original-session" }, limits, storage: null,
    recovery: repository(), identity: () => `local-${++serial}`,
    fetchImpl: async (url, options) => {
      uploads.push({ url, bytes: new Uint8Array(await options.body.get("file").arrayBuffer()) });
      return Response.json({ material_id: `material-${uploads.length}`, sha256: `digest-${uploads.length}` });
    },
    ...options,
    api: { workspaceInputs: emptyHistory, submitWorkspaceInput: async (id, body) => {
      posts.push({ id, body }); return { loop_id: id, accepted_sequence: 1 };
    }, ...api },
  });
  t.after(() => {
    assert.ok(path.resolve(directory).startsWith(path.resolve(os.tmpdir()) + path.sep));
    fs.rmSync(directory, { recursive: true, force: true });
  });
  return { create, repository, directory, uploads, posts, files: () => fs.readdirSync(path.join(directory, "patrol-recovery")).filter(name => name.endsWith(".bin")) };
}

test("restart across API ports restores workspace drafts, reply/version, access mode and exact pathless bytes", async t => {
  const f = fixture(t), first = f.create();
  first.remember({ workspace_id: "a", path: "C:\\a", display_name: "A" });
  first.store("a").edit({ content: "保留正文", input_type: "boundary", request_id: "question", request_revision: 3, file_refs: [{ result_key: "ref" }] });
  const bytes = new Uint8Array([0, 255, 128, 1]);
  await first.add("a", [{ kind: "file", name: "clip.png", type: "image/png", bytes }]);
  first.store("b").edit({ content: "另一个工作区" });
  await first.add(null, [text("尚未绑定")]);
  const second = f.create({ runtime: { apiBase: "http://127.0.0.1:9876", session: "new-session" }, accessMode: "read-only" });
  assert.equal(second.readBinding().workspace_id, "a");
  assert.deepEqual(second.store("a").get().draft, first.store("a").get().draft);
  const local = second.store("a").get().draft.attachments[0].local_id;
  assert.deepEqual(second.source(local), bytes);
  assert.equal(second.store("b").get().draft.content, "另一个工作区");
  assert.equal(second.store(null).get().draft.attachments.length, 1);
  assert.equal(f.uploads.length + f.posts.length, 0);
  second.claimUnbound("b");
  await second.flush();
  const third = f.create();
  assert.equal(third.store(null).get().draft.attachments.length, 0);
  assert.equal(third.store("b").get().draft.attachments.length, 1);
  await third.remove("a", local);
  assert.equal(f.files().length, 1);
});

test("renderer interruption during partial upload resumes missing item with original identity and frozen context", async t => {
  const f = fixture(t), wire = [];
  let secondUploadStarted;
  const started = new Promise(resolve => { secondUploadStarted = resolve; });
  const first = f.create({ fetchImpl: async (url, options) => {
    wire.push({ url, text: await options.body.get("file").text() });
    if (wire.length === 2) { secondUploadStarted(); return new Promise(() => {}); }
    return Response.json({ material_id: "first-material", sha256: "first-digest" });
  } });
  first.store("a").edit({ content: "原要求", input_type: "outcome", request_id: "q", request_revision: 2, file_refs: [{ result_key: "r" }] });
  await first.add("a", [text("第一份"), text("第二份")]);
  void first.submit("a", { loop_id: "loop", live_sequence: 12, fact_refs: [{ fact_id: "fact", revision: 4 }], run_previews: [] });
  await started;
  const original = first.store("a").get().rows[0].request;
  const second = f.create({ accessMode: "read-only", fetchImpl: async (url, options) => {
    wire.push({ url, text: await options.body.get("file").text() });
    return Response.json({ material_id: "second-material", sha256: "second-digest" });
  } });
  assert.equal(second.store("a").get().rows[0].status, "failed");
  assert.equal(Object.isFrozen(second.store("a").get().rows[0].request.visible_context.fact_refs[0]), true);
  second.store("a").edit({ content: "新草稿" });
  await second.retry("a", original.submission_id);
  assert.deepEqual(wire[1], wire[2]);
  assert.equal(wire.length, 3);
  assert.deepEqual(f.posts[0].body, { ...original, attachments: [
    { material_id: "first-material", sha256: "first-digest", source_kind: "text", note: "" },
    { material_id: "second-material", sha256: "second-digest", source_kind: "text", note: "" },
  ] });
  assert.equal(second.store("a").get().draft.content, "新草稿");
  assert.equal(f.files().length, 0);
  assert.equal(f.repository().read().state.submitted.length, 0);
});

test("lost receipt found on an older history page is confirmed without reupload or POST", async t => {
  const f = fixture(t);
  const first = f.create({ api: { submitWorkspaceInput: async () => { throw new Error("回执丢失"); } } });
  await first.add("a", [text("材料")]);
  await assert.rejects(first.submit("a"), /回执丢失/);
  const id = first.store("a").get().rows[0].submission_id;
  first.store("a").edit({ content: "后续草稿" });
  await first.flush();
  const pages = [];
  const second = f.create({ api: { workspaceInputs: async (workspace, before) => {
    pages.push([workspace, before]);
    return before ? { items: [{ submission_id: id, loop_id: "a", content: "", accepted_sequence: 5 }], next_before: null }
      : { items: [{ submission_id: "newer", content: "其他输入" }], next_before: "older" };
  } } });
  const receipt = await second.retry("a", id);
  assert.equal(receipt.status, "accepted");
  assert.deepEqual(pages, [["a", undefined], ["a", "older"]]);
  assert.equal(f.uploads.length, 1);
  assert.equal(f.posts.length, 0);
  assert.equal(second.store("a").get().draft.content, "后续草稿");
  assert.equal(f.files().length, 0);
  assert.equal(f.repository().read().state.payloads.length, 0);
  assert.equal(f.create().hasUnfinished(), true);
});

test("unconfirmed POST retries only after history and reuses the exact persisted wire payload", async t => {
  const f = fixture(t), bodies = [], events = [];
  const first = f.create({ api: { submitWorkspaceInput: async (_id, body) => { bodies.push(body); throw new Error("断网"); } } });
  first.store("a").edit({ content: "稳定请求" });
  await first.add("a", [text("不要重传")]);
  await assert.rejects(first.submit("a"));
  const id = first.store("a").get().rows[0].submission_id;
  const second = f.create({ api: {
    workspaceInputs: async () => { events.push("history"); return { items: [], next_before: null }; },
    submitWorkspaceInput: async (_id, body) => { events.push("post"); bodies.push(body); return { loop_id: "a" }; },
  } });
  assert.equal(events.length, 0, "恢复不能自动重发");
  await second.retry("a", id);
  assert.deepEqual(events, ["history", "post"]);
  assert.deepEqual(bodies[0], bodies[1]);
  assert.equal(f.uploads.length, 1);
  assert.equal(f.create().hasUnfinished(), false);
});

test("failed history reconciliation keeps retry identity and blocks resend and editing", async t => {
  const f = fixture(t), first = f.create({ api: { submitWorkspaceInput: async () => { throw new Error("lost"); } } });
  first.store("a").edit({ content: "原文" });
  await assert.rejects(first.submit("a"));
  const id = first.store("a").get().rows[0].submission_id;
  let offline = true;
  const second = f.create({ api: { workspaceInputs: async () => {
    if (offline) throw new Error("历史暂不可用"); return { items: [], next_before: null };
  } } });
  await assert.rejects(second.retry("a", id), /历史暂不可用/);
  assert.throws(() => second.editFailed("a", id), /历史核对/);
  assert.equal(f.posts.length, 0);
  assert.equal(second.store("a").get().rows[0].submission_id, id);
  offline = false;
  await second.retry("a", id);
  assert.equal(f.posts.length, 1);
});

test("disk write failure retains current edits in memory and blocks uploads and sends", async t => {
  const f = fixture(t), repository = f.repository();
  let full = false;
  const first = f.create({ recovery: { read: () => repository.read(), put: (id, bytes) => repository.put(id, bytes), save: state => {
    if (full) throw new Error("磁盘已满"); return repository.save(state);
  } } });
  first.store("a").edit({ content: "已保存原文" });
  await first.flush();
  full = true;
  first.store("a").edit({ content: "未保存的修改" });
  await assert.rejects(first.flush(), /磁盘已满/);
  await assert.rejects(first.add("a", [text("未保存材料")]), /磁盘已满/);
  assert.equal(first.store("a").get().draft.attachments.length, 1);
  await assert.rejects(first.submit("a"), /磁盘已满/);
  assert.equal(first.store("a").get().rows[0].request.content, "未保存的修改");
  assert.equal(first.store("a").get().rows[0].status, "failed");
  assert.equal(f.uploads.length + f.posts.length, 0);
  assert.equal(f.create().store("a").get().draft.content, "已保存原文");
  assert.equal(f.files().length, 0, "启动清理未提交快照的孤儿附件");
});

test("wire payload must be durable before POST even when uploads completed", async t => {
  const f = fixture(t), repository = f.repository();
  const first = f.create({ recovery: { read: () => repository.read(), put: (id, bytes) => repository.put(id, bytes), save: async state => {
    if (state.submitted.length) throw new Error("提交日志写入失败"); return repository.save(state);
  } } });
  await first.add("a", [text("先保存身份")]);
  await assert.rejects(first.submit("a"), /提交日志写入失败/);
  assert.equal(f.posts.length, 0);
  const id = first.store("a").get().rows[0].submission_id;
  const second = f.create();
  await second.retry("a", id);
  assert.equal(f.uploads.length, 1);
  assert.equal(f.posts[0].body.submission_id, id);
});

test("a send interrupted by disk failure remains retryable in the same renderer once writes recover", async t => {
  const f = fixture(t), repository = f.repository();
  let full = false;
  const intake = f.create({ recovery: { read: () => repository.read(), put: (id, bytes) => repository.put(id, bytes), save: state => {
    if (full) throw new Error("磁盘已满"); return repository.save(state);
  } } });
  intake.store("a").edit({ content: "不能卡在发送中" });
  const sending = intake.submit("a");
  const id = intake.store("a").get().rows[0].submission_id;
  full = true;
  await assert.rejects(sending, /磁盘已满/);
  assert.equal(intake.isSending(), false);
  assert.equal(intake.store("a").get().rows[0].status, "failed");
  full = false;
  const receipt = await intake.retry("a", id);
  assert.equal(receipt.loop_id, "a");
  assert.equal(f.posts[0].body.submission_id, id);
});

test("repository bounds disk bytes, removes interrupted files and preserves referenced materials until release", async t => {
  const f = fixture(t), repository = f.repository();
  await repository.put("../pathless", new Uint8Array(700));
  await repository.save({ version: 1, workspaces: [], source_ids: ["../pathless"] });
  await assert.rejects(repository.put("other", new Uint8Array(400)), /总量超限/);
  const temp = `${repository.sourcePath("interrupted")}.tmp`;
  fs.writeFileSync(temp, new Uint8Array([1]));
  await repository.put("orphan", new Uint8Array(1));
  f.repository().read();
  assert.equal(fs.existsSync(temp), false);
  assert.equal(f.files().length, 1);
  await repository.save({ version: 1, workspaces: [], source_ids: [] });
  assert.equal(f.files().length, 0);
});
