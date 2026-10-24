/* 任务渐进加载回归：真实 app.js、会话渲染与事件路由，配合可控接口和重建式输入节点，
 * 验证慢历史/局部失败不阻塞消息、定向重试、缓存后台刷新、快速切换及焦点归属。
 * 运行：node --test desktop/tests/integration/task-hydration.test.cjs。 */
"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const { createAppHarness, createNode, readAppSource } = require("../helpers/test-helper.cjs");

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function harness() {
  let input = null, markup = "", appWrites = 0;
  const app = createNode();
  const inspector = createNode({
    insertAdjacentHTML(position, html) {
      assert.equal(position, "afterbegin");
      this.innerHTML = html + this.innerHTML;
    },
  });
  const detailStatus = createNode(), skillStatus = createNode(), attachments = createNode();
  const layout = createNode();
  app.querySelector = selector => selector === ".task-workspace-layout" ? layout : null;
  Object.defineProperty(app, "innerHTML", {
    get: () => markup,
    set(value) {
      markup = String(value);
      appWrites++;
      const valueMatch = markup.match(/<textarea id="mainInput"[^>]*>([\s\S]*?)<\/textarea>/);
      input = createNode({
        id: "mainInput", value: valueMatch?.[1] || "", dataset: { skillInput: "main" },
        selectionStart: 0, selectionEnd: 0, selectionDirection: "none",
        focus() { result.document.activeElement = this; },
        setSelectionRange(start, end, direction) {
          this.selectionStart = start; this.selectionEnd = end; this.selectionDirection = direction;
        },
      });
      result.document.activeElement = null;
    },
  });
  const result = createAppHarness({
    statusNode: "record",
    selectors: { "#app": app, "#mainInput": () => input, "#inspectorContent": inspector, "#appInspector": createNode(),
      "#taskDetailLoadStatus": detailStatus, "#mainSkillLoadStatus": skillStatus, "#composerAttachments": attachments },
  });
  const run = source => result.vm.runInContext(source, result.context);
  run(readAppSource());
  run(`state.tasks = ['a', 'b'].map(id => ({task_id:id, thread_id:'th-'+id, workspace_id:'ws', title:id}));
    state.contextTrees.set('ws', []); ensurePluginAssets = async () => {};
    state.view='focus'; state.inspector.open=true; state.inspector.tab='materials';
    var subscriptions=[]; listenToRun=run => subscriptions.push(run.run_id);`);
  const requests = [];
  let respond = path => path.endsWith("/skills") ? { skills: [] } : [];
  result.context.api = (path, options) => {
    requests.push({ path, options });
    return Promise.resolve().then(() => respond(path, options));
  };
  return { ...result, run, app, inspector, detailStatus, skillStatus, attachments, requests,
    respond: handler => { respond = handler; }, input: () => input, appWrites: () => appWrites,
    messages: () => result.document.querySelector("#conversation").innerHTML };
}

const detail = (id, content = `message-${id}`) => ({
  task_id: id, messages: [{ id: `m-${id}`, role: "human", content }], ui_state: {},
  active_run: { run_id: `run-${id}`, status: "running" },
});
const fallback = path => path.endsWith("/skills") ? { skills: [] } : [];
const settle = () => new Promise(resolve => setImmediate(resolve));

test("messages and current run appear while material history is delayed five seconds", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const h = harness();
  h.respond(path => {
    if (path === "/desktop/api/tasks/a") return detail("a");
    if (path.endsWith("/material-history")) return new Promise(resolve => setTimeout(() => resolve([]), 5000));
    return fallback(path);
  });
  await h.run("switchTask('a')");
  assert.match(h.messages(), /message-a/);
  assert.deepEqual(Array.from(h.run("subscriptions")), ["run-a"]);
  assert.match(h.inspector.innerHTML, /材料历史加载中/);
  t.mock.timers.tick(4999);
  await settle();
  assert.equal(h.run("state.materialHistory.has('a')"), false);
  assert.match(h.messages(), /message-a/);
  t.mock.timers.tick(1);
  await settle();
  assert.equal(h.run("state.materialHistory.has('a')"), true);
  assert.doesNotMatch(h.inspector.innerHTML, /材料历史加载中/);
});

for (const [resource, suffix, label, tab] of [
  ["materials", "materials", "材料", "materials"], ["groups", "material-groups", "材料分组", "materials"],
  ["history", "material-history", "材料历史", "materials"], ["agents", "agents", "Agents", "agents"],
  ["skills", "skills", "Skills", "materials"],
]) {
  test(`${label} failure keeps messages usable and retries only that resource`, async () => {
    const h = harness();
    h.run(`state.inspector.tab='${tab}';`);
    let failing = true;
    h.respond(path => {
      if (path === "/desktop/api/tasks/a") return detail("a");
      if (path.endsWith(`/${suffix}`) && failing) throw new Error("unavailable <source>");
      return fallback(path);
    });
    await h.run("switchTask('a')");
    await settle();
    assert.match(h.messages(), /message-a/);
    const statusHtml = resource === "skills" ? h.skillStatus.innerHTML : h.inspector.innerHTML;
    assert.match(statusHtml, new RegExp(`${label}加载失败`));
    assert.match(statusHtml, /unavailable &lt;source&gt;/);
    assert.match(statusHtml, new RegExp(`data-resource="${resource}"`));
    assert.equal(h.statusState.text, "");
    const before = h.requests.length;
    const input = h.input(); input.value = "next step"; input.focus();
    failing = false;
    h.context.__retryButton = { dataset: { action: "retry-task-resource", taskId: "a", resource } };
    await h.run(`handleDocumentClick({target:{closest:selector=>selector==='[data-action]'?__retryButton:null}})`);
    assert.equal(h.requests.length, before + 1);
    assert.equal(h.requests.at(-1).path, `/desktop/api/tasks/a/${suffix}`);
    assert.equal(h.input(), input);
    assert.equal(h.document.activeElement, input);
    assert.doesNotMatch(resource === "skills" ? h.skillStatus.innerHTML : h.inspector.innerHTML, /加载失败/);
  });
}

test("A to B to A reuses pending requests, caches B and keeps A's content, draft and caret", async () => {
  const h = harness();
  const a = deferred(), b = deferred(), historyA = deferred();
  h.respond(path => {
    if (path === "/desktop/api/tasks/a") return a.promise;
    if (path === "/desktop/api/tasks/b") return b.promise;
    if (path === "/desktop/api/tasks/a/material-history") return historyA.promise;
    return fallback(path);
  });
  const firstA = h.run("switchTask('a')");
  const firstInput = h.input(); firstInput.value = "draft-a"; firstInput.focus();
  h.dispatch("input", { target: firstInput });
  const firstB = h.run("switchTask('b')");
  const secondA = h.run("switchTask('a')");
  h.input().focus(); h.input().setSelectionRange(2, 5, "backward");
  await settle();
  assert.equal(h.requests.filter(request => request.path === "/desktop/api/tasks/a").length, 1);
  assert.equal(h.requests.filter(request => request.path === "/desktop/api/tasks/a/material-history").length, 1);
  b.resolve(detail("b"));
  await firstB;
  assert.equal(h.run("state.details.get('b').task_id"), "b");
  assert.doesNotMatch(h.messages(), /message-b/);
  a.resolve(detail("a"));
  await Promise.all([firstA, secondA]);
  assert.match(h.messages(), /message-a/);
  assert.equal(h.run("state.activeTaskId"), "a");
  assert.equal(h.input().value, "draft-a");
  assert.equal(h.document.activeElement, h.input());
  assert.deepEqual([h.input().selectionStart, h.input().selectionEnd, h.input().selectionDirection], [2, 5, "backward"]);
  const writes = h.appWrites(), input = h.input();
  historyA.resolve([{ material_id: "m", message_id: "m-a", relative_path: "notes.txt", ordinal: 0 }]);
  await settle();
  assert.match(h.messages(), /notes.txt/);
  assert.equal(h.appWrites(), writes, "late history updates the conversation without rebuilding the composer");
  assert.equal(h.input(), input);
  assert.equal(h.document.activeElement, input);
});

test("returning to a seen task shows cached content throughout background refresh and failure", async () => {
  const h = harness();
  h.respond(path => path === "/desktop/api/tasks/a" ? detail("a", "cached-a") : fallback(path));
  await h.run("switchTask('a')");
  const refreshed = deferred();
  h.respond(path => path === "/desktop/api/tasks/a" ? refreshed.promise : fallback(path));
  const pending = h.run("switchTask('a')");
  assert.match(h.messages(), /cached-a/);
  assert.match(h.detailStatus.innerHTML, /正在刷新/);
  refreshed.reject(new Error("detail offline"));
  await pending;
  assert.match(h.messages(), /cached-a/);
  assert.match(h.detailStatus.innerHTML, /任务消息与运行状态加载失败/);
  assert.equal(h.statusState.text, "");
  h.respond(path => path === "/desktop/api/tasks/a" ? detail("a", "fresh-a") : fallback(path));
  await h.run("hydrateTaskResource('a', 'detail')");
  assert.match(h.messages(), /fresh-a/);
});

test("first details restore the saved input and delayed groups preserve collapsed custom groups", async () => {
  const h = harness();
  const groups = deferred();
  h.respond(path => {
    if (path === "/desktop/api/tasks/a") return { ...detail("a"), ui_state: { input: "saved draft", collapsed_material_groups: ["custom:g"] } };
    if (path.endsWith("/materials")) return [{ material_id: "m", relative_path: "notes.txt", custom_group_id: "g" }];
    if (path.endsWith("/material-groups")) return groups.promise;
    return fallback(path);
  });
  await h.run("switchTask('a')");
  assert.equal(h.input().value, "saved draft");
  assert.deepEqual(Array.from(h.run("state.details.get('a').ui_state.collapsed_material_groups")), ["custom:g"]);
  groups.resolve([{ group_id: "g", name: "Notes", memberships: [{ material_id: "m", position: 0 }] }]);
  await settle();
  assert.match(h.inspector.innerHTML, /Notes/);
  assert.match(h.inspector.innerHTML, /aria-expanded="false"/);
});

test("switching away before first details cannot mask a saved draft with the loading input", async () => {
  const h = harness(), a = deferred();
  h.respond(path => {
    if (path === "/desktop/api/tasks/a") return a.promise;
    if (path === "/desktop/api/tasks/b") return detail("b");
    return fallback(path);
  });
  const firstA = h.run("switchTask('a')");
  await h.run("switchTask('b')");
  a.resolve({ ...detail("a"), ui_state: { input: "saved-a" } });
  await firstA;
  const returning = h.run("switchTask('a')");
  assert.equal(h.input().value, "saved-a");
  await returning;
  assert.equal(h.input().value, "saved-a");
});

test("late history keeps the must-view recovery controls and the input blocked", async () => {
  const h = harness(), history = deferred();
  h.respond(path => {
    if (path === "/desktop/api/tasks/a") return {
      ...detail("a"), pending_must_view_report: { missing: [{ material_id: "missing" }] },
    };
    if (path.endsWith("/material-history")) return history.promise;
    return fallback(path);
  });
  await h.run("switchTask('a')");
  assert.match(h.messages(), /data-action="retry-must-view"/);
  assert.equal(h.input().disabled, true);
  history.resolve([]);
  await settle();
  assert.match(h.messages(), /data-action="retry-must-view"/);
  assert.match(h.messages(), /data-action="cancel-must-view"/);
  assert.equal(h.input().disabled, true);
});

test("recovered attachments, image requirements and notes survive slow or failed materials", async () => {
  const h = harness(), materials = deferred();
  h.run(`taskRunOperations.freeze('a', 'request-a', {body:'{}', input:'restored input', selectionSignature:JSON.stringify({
    bindings:[{materialId:'image',note:'look here'}], requiredImageIds:['image'], notes:{image:'look here'}
  })});`);
  h.respond(path => {
    if (path === "/desktop/api/tasks/a") return detail("a");
    if (path.endsWith("/materials")) return materials.promise;
    return fallback(path);
  });
  await h.run("switchTask('a')");
  assert.equal(h.run("materialSelection('a').bindings[0].note"), "look here");
  assert.deepEqual(Array.from(h.run("materialSelection('a').requiredImageIds")), ["image"]);
  assert.match(h.app.innerHTML, /本次附件 · 1/);
  materials.reject(new Error("materials offline"));
  await settle();
  assert.equal(h.run("materialSelection('a').bindings[0].materialId"), "image");
  h.respond(() => [{ material_id: "image", relative_path: "recovered.png", is_image: true, size_bytes: 50 }]);
  const input = h.input();
  await h.run("hydrateTaskResource('a', 'materials')");
  assert.match(h.attachments.innerHTML, /recovered.png/);
  assert.deepEqual(Array.from(h.run("materialSelection('a').requiredImageIds")), ["image"]);
  assert.equal(h.run("materialSelection('a').notes.image"), "look here");
  assert.equal(h.input(), input);
});

test("leaving Focus before details arrive does not reopen it or display a late error", async () => {
  const h = harness(), pending = deferred();
  h.respond(path => path === "/desktop/api/tasks/a" ? pending.promise : fallback(path));
  const switching = h.run("switchTask('a')");
  h.run("state.view='patrol'");
  const writes = h.appWrites();
  pending.resolve(detail("a"));
  await switching;
  assert.equal(h.run("state.view"), "patrol");
  assert.equal(h.appWrites(), writes);
  assert.equal(h.statusState.text, "");
});
