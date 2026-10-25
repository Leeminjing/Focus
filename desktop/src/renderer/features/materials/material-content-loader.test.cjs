/*
 * 本文件验证认证材料 Blob loader。输入为可控 fetch、URL API 与材料身份；输出为认证头、缓存复用、
 * 错误传播及按材料/任务/全局释放断言。示例：node --test material-content-loader.test.cjs。
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

const fetches = [];
const revoked = [];
let sequence = 0;
const context = vm.createContext({
  AbortController,
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

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function controlledLoader() {
  const requests = [], revoked = [];
  const scope = vm.createContext({
    AbortController,
    URL: {
      createObjectURL: blob => `blob:${blob.id}`,
      revokeObjectURL: url => revoked.push(url),
    },
  });
  scope.window = scope;
  new vm.Script(fs.readFileSync(require.resolve("./material-content-loader.js"), "utf8")).runInContext(scope);
  const loader = new scope.MaterialContentLoader({
    apiBase: "http://localhost", session: "secret",
    fetchImpl: async (_url, options) => {
      const body = deferred();
      requests.push({ ...body, signal: options.signal });
      // 故意忽略 abort，让迟到的成功/失败仍抵达真实 loader。
      return { ok: true, blob: () => body.promise };
    },
  });
  return { loader, requests, revoked };
}

for (const release of ["releaseMaterial", "releaseTask", "releaseAll"]) {
  for (const outcome of ["success", "failure"]) {
    for (const newFirst of [false, true]) {
      test(`${release}: stale ${outcome}, replacement finishes ${newFirst ? "first" : "last"}`, async () => {
        const { loader, requests, revoked } = controlledLoader();
        const observe = promise => promise.then(url => ({ url }), error => ({ error }));
        const old = observe(loader.load("t", "m"));
        const oldShared = observe(loader.load("t", "m"));
        await new Promise(resolve => setImmediate(resolve));
        loader[release]("t", "m");
        const current = loader.load("t", "m");
        if (newFirst) {
          requests[1].resolve({ id: "current" });
          assert.equal(await current, "blob:current");
        }
        if (outcome === "success") requests[0].resolve({ id: "old" });
        else requests[0].reject(new Error("旧请求失败"));
        const results = await Promise.all([old, oldShared]);
        if (!newFirst) {
          requests[1].resolve({ id: "current" });
          assert.equal(await current, "blob:current");
        }
        const cached = loader.load("t", "m");
        assert.equal(requests.length, 2, "旧请求不得删除新缓存，导致重复请求");
        assert.equal(await cached, "blob:current");
        for (const result of results) {
          assert.match(result.error?.message || "", outcome === "success" ? /已释放/ : /旧请求失败/);
        }
        assert.deepEqual(revoked, outcome === "success" ? ["blob:old"] : []);
        loader.releaseAll();
        assert.deepEqual(revoked, outcome === "success" ? ["blob:old", "blob:current"] : ["blob:current"]);
      });
    }
  }

  test(`${release}: aborts the released request without cancelling its replacement`, async () => {
    const { loader, requests } = controlledLoader();
    const old = loader.load("t", "m");
    const rejected = assert.rejects(old, /旧请求失败/);
    loader[release]("t", "m");
    const current = loader.load("t", "m");
    assert.equal(requests[0].signal?.aborted, true);
    assert.equal(requests[1].signal?.aborted, false);
    assert.notEqual(requests[0].signal, requests[1].signal);
    requests[0].reject(new Error("旧请求失败"));
    requests[1].resolve({ id: "current" });
    await rejected;
    assert.equal(await current, "blob:current");
    loader.releaseAll();
  });
}
