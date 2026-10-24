/* 验证普通任务仓库的真实磁盘耐久、原子替换失败和 preload 的受限同步桥。 */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const { MainRequestStorage, registerMainRequestStorage } = require("./main-request-storage.cjs");
const key = "focus-main-request:task";
const snapshot = id => JSON.stringify({ version: 1, request_id: id, body: '{"message":"原消息"}', editing: false });

function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "focus-main-storage-"));
  t.after(() => {
    assert.ok(path.resolve(directory).startsWith(path.resolve(os.tmpdir()) + path.sep));
    fs.rmSync(directory, { recursive: true, force: true });
  });
  return { directory, storage: new MainRequestStorage(directory) };
}

test("独立仓库实例恢复完整凭证，任务隔离，删除仅影响已确认任务", t => {
  const { directory, storage } = fixture(t);
  assert.equal(storage.getItem(key), null);
  storage.setItem(key, snapshot("original"));
  storage.setItem(`${key}-other`, snapshot("other"));
  const restarted = new MainRequestStorage(directory);
  assert.equal(restarted.getItem(key), snapshot("original"));
  restarted.removeItem(key);
  assert.equal(storage.getItem(key), null);
  assert.equal(storage.getItem(`${key}-other`), snapshot("other"));
});

test("损坏的已提交快照阻止恢复，未提交临时文件不能替换原身份", t => {
  const { storage } = fixture(t);
  storage.setItem(key, snapshot("original"));
  fs.writeFileSync(`${storage.file(key)}.tmp`, snapshot("interrupted"));
  assert.equal(storage.getItem(key), snapshot("original"));
  fs.writeFileSync(storage.file(key), "broken JSON");
  assert.throws(() => storage.getItem(key), SyntaxError);
});

test("原子替换失败仍保留已提交的旧凭证", t => {
  const { storage } = fixture(t);
  storage.setItem(key, snapshot("original"));
  const rename = t.mock.method(fs, "renameSync", () => { throw new Error("rename failed"); });
  assert.throws(() => storage.setItem(key, snapshot("new")), /rename failed/);
  rename.mock.restore();
  assert.equal(storage.getItem(key), snapshot("original"));
});

test("fsync 失败不确认新快照，也不替换旧凭证", t => {
  const { storage } = fixture(t);
  storage.setItem(key, snapshot("original"));
  const sync = t.mock.method(fs, "fsyncSync", () => { throw new Error("fsync failed"); });
  assert.throws(() => storage.setItem(key, snapshot("new")), /fsync failed/);
  sync.mock.restore();
  assert.equal(storage.getItem(key), snapshot("original"));
});

test("非法键、损坏或超限的写入不覆盖原凭证", t => {
  const { storage } = fixture(t);
  storage.setItem(key, snapshot("original"));
  assert.throws(() => storage.setItem("../../outside", snapshot("bad")), /恢复键无效/);
  assert.throws(() => storage.setItem(key, "{}"), /恢复数据无法读取/);
  assert.throws(() => storage.setItem(key, "x".repeat(8 * 1024 * 1024 + 1)), /大小上限/);
  assert.equal(storage.getItem(key), snapshot("original"));
});

test("真实 preload 桥确认磁盘读写，并将授权和存储错误返回渲染器", t => {
  const { directory, storage } = fixture(t);
  const handlers = new Map();
  let allowed = true, desktop;
  registerMainRequestStorage({ on: (channel, fn) => handlers.set(channel, fn) }, {
    directory: () => directory,
    authorize: () => { if (!allowed) throw new Error("unauthorized"); },
  });
  const electron = {
    contextBridge: { exposeInMainWorld: (_name, value) => { desktop = value; } },
    ipcRenderer: { sendSync: (channel, ...args) => {
      const event = {};
      handlers.get(channel)(event, ...args);
      return event.returnValue;
    } },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../preload/preload.cjs"), "utf8"), {
    require: name => { assert.equal(name, "electron"); return electron; },
    process: { argv: [], env: {} }, window: { addEventListener() {} },
  });
  desktop.mainRequestStorage.setItem(key, snapshot("bridged"));
  assert.equal(storage.getItem(key), snapshot("bridged"));
  assert.equal(desktop.mainRequestStorage.getItem(key), snapshot("bridged"));
  allowed = false;
  for (const operation of ["getItem", "setItem", "removeItem"]) {
    assert.throws(() => desktop.mainRequestStorage[operation](key, snapshot("bad")), /unauthorized/);
  }
  assert.equal(storage.getItem(key), snapshot("bridged"));
  allowed = true;
  fs.writeFileSync(storage.file(key), "broken");
  assert.throws(() => desktop.mainRequestStorage.getItem(key), /Unexpected token/);
  desktop.mainRequestStorage.removeItem(key);
  assert.equal(desktop.mainRequestStorage.getItem(key), null);
});
