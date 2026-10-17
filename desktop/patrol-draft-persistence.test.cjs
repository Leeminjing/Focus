/* 验证生产输入/磁盘模块的合并异步保存、发送耐久屏障和历史缓存。
 * 示例：node --test desktop/patrol-draft-persistence.test.cjs。
 */
"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { PatrolRecovery } = require("./patrol-recovery.cjs");
const Intake = require("./workspace-patrol-intake.js");
const Inputs = require("./workspace-patrol-inputs.js");
const limits = { general_file_bytes: 1024, original_image_bytes: 512, run_material_count: 4 };
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

test("100 production draft edits do no synchronous disk work and coalesce into one durable save", async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "focus-draft-save-"));
  const repository = new PatrolRecovery(directory, 1024);
  let syncs = 0, saves = 0;
  const original = fs.fsyncSync;
  t.mock.method(fs, "fsyncSync", (...args) => { syncs++; return original(...args); });
  const intake = Intake.create({ runtime: {}, api: {}, recovery: {
    read: () => repository.read(),
    save: state => { saves++; return repository.save(state); },
  } });
  try {
    for (let i = 0; i < 100; i++) intake.store("a").edit({ content: `draft-${i}` });
    assert.equal(intake.store("a").get().draft.content, "draft-99");
    assert.equal(syncs, 0, "typing must never call fsyncSync");
    assert.equal(saves, 0, "typing must not serialize and save on each edit");
    await intake.flush();
    assert.equal(saves, 1);
    assert.equal(syncs, 0);
    assert.equal(repository.read().state.workspaces[0][1].draft.content, "draft-99");
  } finally {
    // 测试目录固定来自 mkdtemp，不接受外部路径。
    assert.ok(path.resolve(directory).startsWith(path.resolve(os.tmpdir()) + path.sep));
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test("draft changes reuse history rows and snapshots until history changes", () => {
  const store = Inputs.create(() => "send");
  store.history({ items: [{ submission_id: "old", content: "history", created_at: "2020" }], next_before: null });
  const before = store.get(), saved = store.snapshot();
  for (let i = 0; i < 100; i++) store.edit({ content: String(i) });
  assert.equal(store.get().rows, before.rows);
  assert.equal(store.snapshot().rows, saved.rows);
  assert.equal(store.get().historyRevision, before.historyRevision);
  assert.notEqual(store.get().draftRevision, before.draftRevision);
  store.submit();
  assert.notEqual(store.get().rows, before.rows);
  assert.notEqual(store.get().historyRevision, before.historyRevision);
});

test("fixed save window coalesces edits and saves while typing continues", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const snapshots = [];
  const intake = Intake.create({ runtime: {}, api: {}, recovery: { read: () => ({}), save: async state => snapshots.push(state) } });
  intake.store("a").edit({ content: "first" });
  t.mock.timers.tick(200);
  intake.store("a").edit({ content: "second" });
  assert.equal(snapshots.length, 0);
  t.mock.timers.tick(50);
  await tick();
  assert.equal(snapshots.length, 1);
  assert.equal(snapshots[0].workspaces[0][1].draft.content, "second");
  intake.store("a").edit({ content: "third" });
  t.mock.timers.tick(250);
  await tick();
  assert.equal(snapshots.length, 2);
});

test("an edit during a slow save is saved after it with no overlapping writes", async () => {
  const gate = deferred(), snapshots = [];
  let active = 0;
  const intake = Intake.create({ runtime: {}, api: {}, recovery: { read: () => ({}), save: async state => {
    assert.equal(active++, 0);
    snapshots.push(state);
    if (snapshots.length === 1) await gate.promise;
    active--;
  } } });
  intake.store("a").edit({ content: "old" });
  const first = intake.flush();
  intake.store("a").edit({ content: "new" });
  const second = intake.flush();
  assert.equal(snapshots.length, 1);
  gate.resolve(); await Promise.all([first, second]);
  assert.equal(snapshots.length, 2);
  assert.equal(snapshots[1].workspaces[0][1].draft.content, "new");
});

test("background save failure reports an error without losing edits; flush can retry the latest draft", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const errors = [];
  let full = true, saved;
  const intake = Intake.create({ runtime: {}, api: {}, onSaveError: error => errors.push(error.message),
    recovery: { read: () => ({}), save: async state => { if (full) throw new Error("disk full"); saved = state; } } });
  intake.store("a").edit({ content: "unsaved" });
  t.mock.timers.tick(250); await tick();
  assert.deepEqual(errors, ["disk full"]);
  assert.equal(intake.store("a").get().draft.content, "unsaved");
  intake.store("a").edit({ content: "latest" });
  full = false; await intake.flush();
  assert.equal(saved.workspaces[0][1].draft.content, "latest");
});

test("uploads and POST each wait for their durable request and attachment references", async () => {
  const gates = [deferred(), deferred(), deferred()], snapshots = [], posts = [], uploads = [];
  let block = false, barriers = 0;
  const intake = Intake.create({ runtime: {}, limits, identity: (() => { let id = 0; return () => `id-${++id}`; })(),
    recovery: { read: () => ({}), put: async () => {}, save: async state => {
      snapshots.push(state);
      if (block && barriers < gates.length) await gates[barriers++].promise;
    } },
    fetchImpl: async () => { uploads.push(true); return Response.json({ material_id: "material", sha256: "digest" }); },
    api: { submitWorkspaceInput: async (_id, body) => { posts.push(body); return { loop_id: "loop" }; } },
  });
  intake.store("a").edit({ content: "frozen", file_refs: [{ result_key: "file-version" }], request_id: "question", request_revision: 2 });
  await intake.add("a", [{ kind: "text", text: "attachment" }]);
  block = true;
  const sending = intake.submit("a");
  await tick();
  assert.equal(uploads.length + posts.length, 0);
  const request = snapshots.at(-1).workspaces[0][1].rows[0].request;
  assert.equal(request.content, "frozen");
  assert.equal(request.request_revision, 2);
  assert.equal(snapshots.at(-1).source_ids.length, 1);
  gates[0].resolve(); await tick();
  assert.equal(uploads.length, 1);
  assert.equal(posts.length, 0);
  // 第二个屏障保存已上传材料；投递前还必须保存确切 wire payload。
  gates[1].resolve(); await tick();
  assert.equal(posts.length, 0, "POST 必须等待确切 wire payload 写入成功");
  gates[2].resolve(); await sending;
  assert.equal(posts[0].submission_id, request.submission_id);
  assert.deepEqual(posts[0].file_refs, request.file_refs);
  const durablePost = snapshots.find(state => state.submitted?.length);
  assert.deepEqual(durablePost.payloads[0][1], posts[0]);
});

test("a queued older snapshot cannot delete attachment bytes awaiting their first reference", async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "focus-draft-order-"));
  const repository = new PatrolRecovery(directory, 1024);
  try {
    const put = repository.put("new", new Uint8Array([1, 2]));
    const old = repository.save({ version: 1, workspaces: [], source_ids: [] });
    const latest = repository.save({ version: 1, workspaces: [], source_ids: ["new"] });
    await Promise.all([put, old, latest]);
    assert.deepEqual(repository.read().sources, [["new", new Uint8Array([1, 2])]]);
    await repository.save({ version: 1, workspaces: [], source_ids: [] });
    assert.equal(fs.existsSync(repository.sourcePath("new")), false);
  } finally {
    assert.ok(path.resolve(directory).startsWith(path.resolve(os.tmpdir()) + path.sep));
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test("renderer reload during an async save leaves its in-flight temporary file intact", async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "focus-draft-reload-"));
  const repository = new PatrolRecovery(directory, 1024);
  const gate = deferred(), started = deferred();
  try {
    repository.read();
    await repository.save({ version: 1, workspaces: [], source_ids: [], marker: "old" });
    const rename = fs.promises.rename;
    t.mock.method(fs.promises, "rename", async (...args) => { started.resolve(); await gate.promise; return rename(...args); });
    const saving = repository.save({ version: 1, workspaces: [], source_ids: [], marker: "new" });
    await started.promise;
    assert.equal(repository.read().state.marker, "old");
    assert.equal(fs.existsSync(`${repository.snapshotPath}.tmp`), true);
    gate.resolve(); await saving;
    assert.equal(repository.read().state.marker, "new");
  } finally {
    gate.resolve();
    await repository.writes;
    assert.ok(path.resolve(directory).startsWith(path.resolve(os.tmpdir()) + path.sep));
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
