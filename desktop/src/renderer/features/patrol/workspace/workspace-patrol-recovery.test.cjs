/* 验证生产工作台装配的恢复接线，重建入口后保留正文、附件与失败提交身份。
 * 示例：node --test desktop/src/renderer/features/patrol/workspace/workspace-patrol-recovery.test.cjs。
 */
"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const Intake = require("./workspace-patrol-intake.js");
const { PatrolRecovery } = require("../../../../main/patrol-recovery.cjs");

test("workbench assembly restores drafts, bytes and failed request identity independently of the robot", async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "focus-workbench-recovery-"));
  t.after(() => {
    assert.ok(path.resolve(directory).startsWith(path.resolve(os.tmpdir()) + path.sep));
    fs.rmSync(directory, { recursive: true, force: true });
  });
  const storage = new Map();
  let serial = 0;
  const bodies = [];
  const api = { workspaceInputs: async () => ({ items: [], next_before: null }), submitWorkspaceInput: async (_id, body) => {
    bodies.push(body); throw new Error("lost receipt");
  } };
  const create = async () => {
    const repository = new PatrolRecovery(directory, 1024);
    const source = fs.readFileSync(path.join(__dirname, "../../../app/app.js"), "utf8");
    const assembly = source.match(/async function createWorkspacePatrolIntake\(\) \{[\s\S]*?\r?\n\}/)[0];
    return vm.runInNewContext(`${assembly}\ncreateWorkspacePatrolIntake()`, {
      window: { FocusWorkspacePatrolIntake: { create: options => Intake.create({
        ...options, storage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
        identity: () => `id-${++serial}`, limits: { general_file_bytes: 1024, original_image_bytes: 512, run_material_count: 4 },
        fetchImpl: async () => Response.json({ material_id: "material", sha256: "digest" }),
      }) }, focusDesktop: { workspacePatrolRecovery: repository }, localStorage: storage },
      runtime: {}, loopApi: api, accessMode: { readNewSessionDefault: () => "workspace-write" }, setStatus: () => {},
    });
  };
  const first = await create();
  first.remember({ workspace_id: "a", path: "/a" });
  first.store("a").edit({ content: "failed request" });
  await first.add("a", [{ kind: "text", text: "failed attachment" }]);
  await assert.rejects(first.submit("a"), /lost receipt/);
  const failed = first.store("a").get().rows[0];
  first.store("a").edit({ content: "unsent draft", request_id: "q", request_revision: 2 });
  await first.add("a", [{ kind: "file", name: "draft.bin", bytes: new Uint8Array([0, 255, 128]) }]);
  await first.flush();
  const second = await create();
  assert.equal(second.readBinding().workspace_id, "a");
  assert.deepEqual(second.store("a").get().draft, first.store("a").get().draft);
  const local = second.store("a").get().draft.attachments[0].local_id;
  assert.deepEqual(second.source(local), new Uint8Array([0, 255, 128]));
  assert.deepEqual(second.store("a").get().rows[0].request, failed.request);
  assert.equal(second.store("a").get().rows[0].status, "failed");
  assert.equal(bodies.length, 1, "restoring must not resend");
  await assert.rejects(second.retry("a", failed.submission_id), /lost receipt/);
  assert.deepEqual(bodies[1], bodies[0]);
});
