/*
 * 本文件验证认证材料 Blob loader。输入为可控 fetch、URL API 与材料身份；输出为认证头、缓存复用、
 * 错误传播及按材料/任务/全局释放断言。示例：node --test material-content-loader.test.cjs。
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const fetches = [];
const revoked = [];
let sequence = 0;
const context = vm.createContext({
  fetch: async (url, options) => {
    fetches.push({ url, options });
    return { ok: true, status: 200, blob: async () => ({}) };
  },
  URL: {
    createObjectURL() { sequence += 1; return `blob:test-${sequence}`; },
    revokeObjectURL(url) { revoked.push(url); },
  },
});
context.window = context;
new vm.Script(fs.readFileSync(require.resolve("./material-content-loader.js"), "utf8")).runInContext(context);

(async () => {
  const loader = new context.MaterialContentLoader({ apiBase: "http://localhost/", session: "secret" });
  const first = await loader.load("t1", "m1");
  const again = await loader.load("t1", "m1");
  assert.equal(first, again);
  assert.equal(fetches.length, 1);
  assert.match(fetches[0].url, /\/tasks\/t1\/materials\/m1\/content$/);
  assert.equal(fetches[0].options.headers["X-Focus-Session"], "secret");
  assert.doesNotMatch(fetches[0].url, /secret|session=/);
  await loader.load("t1", "m2");
  await loader.load("t2", "m3");
  loader.releaseMaterial("t1", "m1");
  loader.releaseTask("t1");
  loader.releaseAll();
  assert.deepEqual(revoked.sort(), ["blob:test-1", "blob:test-2", "blob:test-3"]);

  const failed = new context.MaterialContentLoader({
    apiBase: "http://localhost",
    session: "secret",
    fetchImpl: async () => ({ ok: false, status: 404 }),
  });
  await assert.rejects(() => failed.load("t", "missing"), /404/);

  let finishBlob;
  const racing = new context.MaterialContentLoader({
    apiBase: "http://localhost",
    session: "secret",
    fetchImpl: async () => ({
      ok: true,
      status: 200,
      blob: () => new Promise(resolve => { finishBlob = resolve; }),
    }),
  });
  const pending = racing.load("t", "racing");
  await new Promise(resolve => setImmediate(resolve));
  racing.releaseMaterial("t", "racing");
  finishBlob({});
  await assert.rejects(() => pending, /已释放/);
  assert.equal(revoked.at(-1), "blob:test-4");
  console.log("material-content-loader: all assertions passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
