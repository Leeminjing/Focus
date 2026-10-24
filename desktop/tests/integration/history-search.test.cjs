"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { createAppHarness, readAppSource } = require("../helpers/test-helper.cjs");

function harness() {
  const h = createAppHarness();
  h.vm.runInContext(readAppSource(), h.context);
  h.run = code => h.vm.runInContext(code, h.context);
  return h;
}

test("search escapes content, highlights literal matches and distinguishes archives", () => {
  const h = harness();
  const html = h.run(`
    state.mapQuery = '登录报错';
    state.historySearch.results = [{ title: '<img onerror=evil>', context_id: 'archive', workspace_name: 'work', lifecycle: 'archived', kind: 'message', key: 'message:0', snippet: '<script>登录报错</script>' }];
    renderMap();
    app.innerHTML;
  `);
  assert.match(html, /&lt;script&gt;<mark>登录报错<\/mark>&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<img onerror/);
  assert.match(html, /已归档 · 只读/);
  assert.match(html, /open-history-result/);
});

test("older search responses cannot replace a newer query", async () => {
  const h = harness();
  const result = await h.run(`(async () => {
    let resolveOld;
    api = path => path.includes('old') ? new Promise(resolve => { resolveOld = resolve; }) : Promise.resolve({ results: [{ title: 'new' }], has_more: false, unavailable_context_ids: [] });
    state.mapQuery = 'old';
    const old = loadHistorySearch();
    state.mapQuery = 'new';
    await loadHistorySearch();
    resolveOld({ results: [{ title: 'old' }], has_more: true, unavailable_context_ids: [] });
    await old;
    return state.historySearch.results[0].title;
  })()`);
  assert.equal(result, "new");
});

test("archived reader uses only GET, targets an old message and has no composer", async () => {
  const h = harness();
  const result = await h.run(`(async () => {
    const requests = [];
    api = async (path, options) => {
      requests.push({ path, method: options?.method || 'GET' });
      return { context_id: 'archive', title: '归档任务', workspace_name: 'work', lifecycle: 'archived', materials: [], entries: Array.from({ length: 230 }, (_, index) => ({ key: 'message:' + index, message_id: 'id:' + index, role: 'human', text: '<b>登录报错 ' + index + '</b>' })) };
    };
    state.view = 'map';
    await openHistory('archive', 'message:150', 'id:150');
    return { html: app.innerHTML, requests, start: state.historyReader.start, activeTaskId: state.activeTaskId };
  })()`);
  assert.equal(result.requests.length, 1);
  assert.equal(result.requests[0].path, "/desktop/api/history/archive");
  assert.equal(result.requests[0].method, "GET");
  assert.equal(result.start, 130);
  assert.match(result.html, /history-target/);
  assert.match(result.html, /&lt;b&gt;登录报错 150/);
  assert.match(result.html, /history-earlier/);
  assert.match(result.html, /history-later/);
  assert.doesNotMatch(result.html, /mainInput|history-continue|textarea|unarchive/);
  assert.equal(result.activeTaskId, null);
});

test("leaving the reader prevents a late response from replacing the page", async () => {
  const h = harness();
  const view = await h.run(`(async () => {
    let resolve;
    api = () => new Promise(done => { resolve = done; });
    const pending = openHistory('archive');
    state.view = 'map';
    resolve({ title: 'late' });
    await pending;
    return state.view;
  })()`);
  assert.equal(view, "map");
});
