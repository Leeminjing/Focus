"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const { createHash } = require("node:crypto");
const { createArtifactFiles } = require("./artifact-files.cjs");

test("native file exits preserve report bytes and revalidate after a save dialog", async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "focus-artifact-"));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const root = path.join(directory, "workspace"), output = path.join(directory, "报告副本.pdf");
  await fs.mkdir(root);
  const source = path.join(root, "报告.pdf"), bytes = Buffer.from([37,80,68,70,0,255,17]);
  await fs.writeFile(source, bytes);
  const reference = { loopId: "loop", factId: "fact", revision: 2 };
  const file = { root_path: root, path: source, sha256: createHash("sha256").update(bytes).digest("hex") };
  const opened = [], revealed = [];
  let save = async () => ({ filePath: output });
  const operations = createArtifactFiles({ resolve: async ref => { assert.deepEqual(ref, reference); return file; },
    shell: { openPath: async value => { opened.push(value); return ""; }, showItemInFolder: value => revealed.push(value) },
    dialog: { showSaveDialog: (...args) => save(...args) } });
  await operations.open(reference); await operations.reveal(reference); await operations.saveAs(reference);
  assert.deepEqual(await fs.readFile(output), bytes);
  assert.deepEqual(opened, [source]); assert.deepEqual(revealed, [source]);
  save = async () => ({ canceled: true });
  assert.deepEqual(await operations.saveAs(reference), { canceled: true });
  save = async () => ({ filePath: source });
  await assert.rejects(operations.saveAs(reference), /不同的另存位置/);
  save = async () => { await fs.writeFile(source, "changed while dialog was open"); return { filePath: output }; };
  await assert.rejects(operations.saveAs(reference), /已有更新/);
  assert.deepEqual(await fs.readFile(output), bytes);
  file.path = output;
  await assert.rejects(operations.open(reference), /超出工作区/);
  await assert.rejects(operations.open({ ...reference, revision: -1 }), /无效/);
  file.path = source; await fs.unlink(source);
  await assert.rejects(operations.reveal(reference), /ENOENT/);
});

test("OS open failures are surfaced to the drawer", async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "focus-artifact-open-"));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const source = path.join(root, "report.txt"); await fs.writeFile(source, "report");
  const operations = createArtifactFiles({ resolve: async () => ({ root_path: root, path: source, sha256: createHash("sha256").update("report").digest("hex") }),
    shell: { openPath: async () => "没有默认应用" }, dialog: {} });
  await assert.rejects(operations.open({ loopId: "loop", factId: "fact", revision: 1 }), /没有默认应用/);
});
