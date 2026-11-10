/* Queries stay read-only; loading updates registry state without claiming UI/API
 * activation or changing active Run snapshots. Follow-up query failures retain
 * the confirmed POST result and keep query/load errors separate.
 */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { createAppHarness, readAppSource } = require("../helpers/test-helper.cjs");

function fixture() {
  const h = createAppHarness();
  const run = source => h.vm.runInContext(source, h.context);
  run(readAppSource());
  run(`
    state.view = 'plugins';
    state.plugins.layout = 'list';
    state.plugins.query = 'demo';
    state.plugins.filter = 'active';
    state.plugins.selectedName = 'demo';
    state.details.set('task', { active_run: { run_id: 'running', tool_snapshot: ['old-tool'] } });
    var activation = { registry: 'immediate', tools_and_hooks: 'next_run', running_runs: 'unchanged',
      desktop_assets: 'process_restart', http_routes: 'process_restart' };
    var response = { plugins: [{ name: 'demo', version: '2', status: 'active', injected: ['tool'],
      desktop_assets: ['entry.js'] }], interfaces: { tool: { plugins: ['demo'] } }, activation };
    var calls = [], assetLoads = 0, queryFails = false, reloadFails = false;
    ensurePluginAssets = () => { assetLoads += 1; };
    api = async (path, options = {}) => {
      calls.push({ path, method: options.method || 'GET' });
      if (options.method === 'POST') {
        if (reloadFails) throw new Error('load response lost');
        return response;
      }
      if (queryFails) throw new Error('query unavailable');
      if (path.endsWith('/traces')) return { traces: [{ plugin: 'demo', status: 'success' }] };
      return response;
    };
  `);
  return { ...h, run, value: source => JSON.parse(JSON.stringify(run(source))) };
}

test("refresh action only queries status/traces and preserves activation, selection and layout", async () => {
  const h = fixture();
  await h.run(`handleDocumentClick({ target: { closest: selector =>
    selector === '[data-action]' ? { dataset: { action: 'refresh-plugins' } } : null } })`);
  assert.deepEqual(h.value("calls"), [
    { path: "/desktop/api/plugins", method: "GET" },
    { path: "/desktop/api/plugins/traces", method: "GET" },
  ]);
  assert.equal(h.run("assetLoads"), 0);
  assert.deepEqual(h.value("state.plugins.activation"), h.value("activation"));
  assert.equal(h.run("state.plugins.layout"), "list");
  assert.equal(h.run("state.plugins.selectedName"), "demo");
  assert.equal(h.run("state.plugins.queried"), true);
});

test("confirmed load survives failed status query without remounting assets or changing active Runs", async () => {
  const h = fixture();
  h.run("queryFails = true");
  await h.run("reloadPlugins()");
  assert.equal(h.run("state.plugins.reloadSucceeded"), true);
  assert.equal(h.run("state.plugins.reloadError"), null);
  assert.equal(h.run("state.plugins.error"), "query unavailable");
  assert.equal(h.run("state.plugins.plugins[0].version"), "2");
  assert.deepEqual(h.value("state.plugins.activation"), h.value("activation"));
  assert.deepEqual(h.value("state.details.get('task').active_run"), { run_id: "running", tool_snapshot: ["old-tool"] });
  assert.equal(h.run("assetLoads"), 0);
  assert.equal(h.run("state.plugins.loading || state.plugins.reloading"), false);
  assert.equal(h.run("calls.filter(c => c.method === 'POST').length"), 1);
  h.run("queryFails = false");
  await h.run("hydratePlugins()");
  assert.equal(h.run("state.plugins.error"), null);
  assert.equal(h.run("state.plugins.reloadSucceeded"), true);
  assert.equal(h.run("calls.filter(c => c.method === 'POST').length"), 1);
});

test("load failure is unconfirmed, retains the old registry and is not retried by refresh", async () => {
  const h = fixture();
  h.run("state.plugins.plugins = [{name:'old', status:'active'}]; reloadFails = true");
  await h.run("reloadPlugins()");
  assert.equal(h.run("state.plugins.reloadSucceeded"), false);
  assert.equal(h.run("state.plugins.reloadError"), "load response lost");
  assert.equal(h.run("state.plugins.plugins[0].name"), "old");
  assert.equal(h.run("calls.length"), 1);
  await h.run("hydratePlugins()");
  assert.equal(h.run("calls.filter(c => c.method === 'POST').length"), 1);
  assert.equal(h.run("state.plugins.reloadError"), "load response lost");
});

test("duplicate actions during loading are ignored and completion does not navigate back", async () => {
  const h = fixture();
  h.run(`
    var finishLoad;
    var originalApi = api;
    api = async (path, options = {}) => options.method === 'POST'
      ? new Promise(resolve => { calls.push({path, method:'POST'}); finishLoad = () => resolve(response); })
      : originalApi(path, options);
    var first = reloadPlugins();
  `);
  assert.equal(h.run("state.plugins.reloading"), true);
  await h.run("reloadPlugins()");
  await h.run("hydratePlugins()");
  assert.equal(h.run("calls.length"), 1);
  h.run("state.view = 'memory'; finishLoad()");
  await h.run("first");
  assert.equal(h.run("state.view"), "memory");
  assert.equal(h.run("state.plugins.reloadSucceeded"), true);
});
