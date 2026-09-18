/*
 * 本文件对外提供作曲区未发送内容的归属与持久化检查。输入为草稿镜像模块、脚手架里的会话状态与真实
 * app.js 渲染/输入路径；输出为七类断言结果：镜像按任务归属（互不串味、回落取持久化值）、打字即归属、
 * 切换访问模式后内容仍在、切换界面语言后内容仍在、任务往返各自保留、发送成功后清空且重建不复活、页面
 * 隐藏与卸载流程各补一次落盘。工作流只构造数据并调用既有函数，不修改运行时代码。
 * 脚手架对作曲区的建模与浏览器语义一致：渲染（`#app` 被重新赋值）会替换掉旧输入框，因此输入框取值只可能
 * 来自"这一次渲染写进标记的值"或用户本次输入——这正是本变更要修的失效形态。
 * 示例：`node desktop/composer-draft.test.cjs`。
 */
"use strict";

const assert = require("node:assert");
const test = require("node:test");
const path = require("node:path");
const { createAppHarness, readAppSource } = require(path.resolve(__dirname, "test-helper.cjs"));
const { createElement } = require(path.resolve(__dirname, "test-dom.cjs"));

function buildApp() {
  const conversation = createElement("div");
  let markup = "";
  let typed = null;
  const renderedValue = () => {
    const match = markup.match(/<textarea id="mainInput"[^>]*>([\s\S]*?)<\/textarea>/);
    return match ? match[1] : "";
  };
  const mainInput = createElement("textarea");
  mainInput.id = "mainInput";
  Object.defineProperty(mainInput, "value", {
    get() { return typed !== null ? typed : renderedValue(); },
    set(next) { typed = String(next ?? ""); },
  });
  const appNode = createElement("div");
  Object.defineProperty(appNode, "innerHTML", {
    get() { return markup; },
    set(next) { markup = String(next ?? ""); typed = null; },
  });
  appNode.querySelector = selector => (selector === "#conversation" ? conversation : null);
  // window 级监听器在脚手架里默认不可派发（window === 全局对象）；收集起来供本文件按需派发
  const windowListeners = new Map();
  // 定时器同样默认是空实现：收集起来，使"去抖落盘"这条路径可被确定性触发
  const timers = { pending: [], cleared: new Set(), nextId: 1 };
  const harness = createAppHarness({
    fetch: true,
    statusNode: "record",
    selectors: { "#mainInput": mainInput, "#app": appNode, "#conversation": conversation },
    globals: {
      addEventListener(type, handler) {
        windowListeners.set(type, [...(windowListeners.get(type) || []), handler]);
      },
      setTimeout(callback, delay) {
        const id = timers.nextId++;
        timers.pending.push({ id, callback, delay });
        return id;
      },
      clearTimeout(id) { timers.cleared.add(id); },
    },
  });
  harness.mainInput = mainInput;
  harness.timers = timers;
  harness.pendingTimers = () => timers.pending.filter(timer => !timer.fired && !timers.cleared.has(timer.id));
  harness.runTimers = () => {
    const due = harness.pendingTimers();
    for (const timer of due) {
      timer.fired = true;
      timer.callback();
    }
    return due.length;
  };
  harness.dispatchWindow = (type, event) => {
    for (const handler of windowListeners.get(type) || []) handler(event);
  };
  harness.vm.runInContext(readAppSource(), harness.context);
  harness.vm.runInContext(`
    globalThis.__task = { task_id: "task-a", thread_id: "th-a", workspace_id: "ws", title: "A", harness_mode: "workspace", workspace_path: "C:\\\\ws", ui_state: {} };
    globalThis.__taskB = { task_id: "task-b", thread_id: "th-b", workspace_id: "ws", title: "B", harness_mode: "workspace", workspace_path: "C:\\\\ws", ui_state: {} };
    state.view = "focus";
    state.tasks = [__task, __taskB];
    state.activeTaskId = __task.task_id;
    state.details = new Map([
      [__task.task_id, { messages: [], ui_state: {} }],
      [__taskB.task_id, { messages: [], ui_state: {} }],
    ]);
    state.materialHistory = new Map();
    state.materialGroups = new Map();
    state.materials = new Map();
    state.agents = new Map();
    state.streams = new Map();
    state.streamBuffers = new Map();
    state.streamFrames = new Map();
    renderFocus(__task);
  `, harness.context);
  return harness;
}

const composerValue = harness => harness.mainInput.value;

function type(harness, value) {
  harness.mainInput.value = value;
  harness.dispatch("input", { target: harness.mainInput });
}

test("镜像按任务归属：写入、读取、回落与释放在同一任务上自洽", () => {
  const harness = createAppHarness({ fetch: true });
  const draft = harness.vm.runInContext(`(() => {
    const api = window.FocusComposerDraft;
    const written = api.claim("task-a", "还没发的一句话");
    const read = api.value("task-a", "fallback");
    const other = api.value("task-b", "fallback");
    const beforeRelease = api.size();
    api.release("task-a");
    return { written, read, other, beforeRelease, afterRelease: api.size(), released: api.value("task-a", "fallback") };
  })()`, harness.context);
  assert.equal(draft.written, "还没发的一句话");
  assert.equal(draft.read, "还没发的一句话", "写入后读回同一内容");
  assert.equal(draft.other, "fallback", "别的任务不受影响（按归属隔离）");
  assert.equal(draft.beforeRelease, 1);
  assert.equal(draft.afterRelease, 0, "释放后镜像不再持有该任务");
  assert.equal(draft.released, "fallback", "释放后回落到调用方给的持久化取值");
});

test("打字即归属：输入事件把内容写进该任务的镜像，不需要任何保存动作", () => {
  const harness = buildApp();
  type(harness, "未发送的草稿正文");
  const mirrored = harness.vm.runInContext("FocusComposerDraft.value('task-a', null)", harness.context);
  assert.equal(mirrored, "未发送的草稿正文", "输入事件后镜像立即持有内容");
});

test("切换本机资源访问模式不丢未发送内容", () => {
  const harness = buildApp();
  type(harness, "未发送的草稿正文");
  assert.equal(composerValue(harness), "未发送的草稿正文", "前置：输入框里有内容");
  harness.vm.runInContext("applyAccessMode('main', 'full')", harness.context);
  const afterFull = harness.vm.runInContext("state.details.get('task-a').ui_state.access_mode", harness.context);
  assert.equal(afterFull, "full", "前置：模式确实切到了完全权限");
  assert.equal(composerValue(harness), "未发送的草稿正文", "放大后内容仍在");
  harness.vm.runInContext("applyAccessMode('main', 'workspace')", harness.context);
  assert.equal(composerValue(harness), "未发送的草稿正文", "收窄后内容仍在");
});

test("切换界面语言不丢未发送内容", () => {
  const harness = buildApp();
  type(harness, "语言切换前的草稿");
  harness.dispatch("focus:languagechange", {});
  assert.equal(composerValue(harness), "语言切换前的草稿", "语言切换重建界面后内容仍在");
});

test("任务往返各自保留：A 的内容不带进 B，B 也不覆盖 A", () => {
  const harness = buildApp();
  type(harness, "任务 A 的草稿");
  harness.vm.runInContext(`
    state.activeTaskId = __taskB.task_id;
    renderFocus(__taskB);
  `, harness.context);
  assert.equal(composerValue(harness), "", "切到任务 B 时输入框为空（不串味）");
  type(harness, "任务 B 的草稿");
  harness.vm.runInContext(`
    state.activeTaskId = __task.task_id;
    renderFocus(__task);
  `, harness.context);
  assert.equal(composerValue(harness), "任务 A 的草稿", "回到任务 A 时其未发送内容仍在");
});

test("发送成功后清空且重建不复活", async () => {
  const harness = buildApp();
  type(harness, "这一条要发出去");
  await harness.vm.runInContext("sendMain()", harness.context);
  assert.equal(composerValue(harness), "", "发送后作曲区为空");
  assert.equal(
    harness.vm.runInContext("FocusComposerDraft.value('task-a', null)", harness.context), "",
    "镜像随渲染后的空输入框归零，不保留已发送内容",
  );
  harness.vm.runInContext("renderFocus(__task)", harness.context);
  assert.equal(composerValue(harness), "", "再次重建仍为空");
});

test("打字期间到达的任务刷新不丢内容，也不写进别的任务", async () => {
  const harness = buildApp();
  // 服务器视图里没有未发送内容：刷新会把 ui_state 覆盖成服务端版本
  const detailOf = taskId => ({ task_id: taskId, messages: [], ui_state: {} });
  harness.context.fetch = async url => {
    const path = String(url);
    const payload = path.includes("/agents") ? []
      : path.includes("/materials") ? []
      : path.includes("/material-groups") ? []
      : path.includes("/material-history") ? []
      : path.includes("/skills") ? { skills: [] }
      : detailOf(path.includes("task-b") ? "task-b" : "task-a");
    return { ok: true, status: 200, json: async () => payload };
  };
  type(harness, "打字期间到达刷新时的草稿");
  await harness.vm.runInContext("hydrateActive('task-a')", harness.context);
  const afterRefresh = harness.vm.runInContext(`(() => ({
    serverInput: state.details.get('task-a').ui_state?.input,
    mirrored: FocusComposerDraft.value('task-a', null),
  }))()`, harness.context);
  assert.equal(afterRefresh.serverInput, undefined, "前置：服务端视图里没有这段未发送内容");
  assert.equal(afterRefresh.mirrored, "打字期间到达刷新时的草稿", "刷新不得清掉本任务的未发送内容");
  assert.equal(composerValue(harness), "打字期间到达刷新时的草稿", "刷新后作曲区仍是同一段内容");

  await harness.vm.runInContext("hydrateActive('task-b')", harness.context);
  const otherTask = harness.vm.runInContext(`(() => ({
    bInput: state.details.get('task-b').ui_state?.input,
    aMirror: FocusComposerDraft.value('task-a', null),
  }))()`, harness.context);
  assert.equal(otherTask.bInput, undefined, "别的任务不得被写入本任务的内容");
  assert.equal(otherTask.aMirror, "打字期间到达刷新时的草稿", "别的任务刷新不影响本任务镜像");
});

test("打字后由去抖定时器落盘，显式 flush 清掉待触发定时器（不重复落盘）", () => {
  const harness = buildApp();
  type(harness, "去抖落盘的草稿");
  assert.equal(harness.pendingTimers().length, 1, "打字排入一次去抖落盘");
  assert.equal(harness.runTimers(), 1, "去抖定时器可被触发");
  const puts = harness.fetches.filter(call => String(call.url).includes("/ui-state"));
  assert.equal(puts.length, 1, "定时器触发后落盘一次");
  assert.match(String(puts.at(-1).options.body), /去抖落盘的草稿/, "落盘内容包含未发送内容");

  type(harness, "去抖落盘的草稿（第二版）");
  assert.equal(harness.pendingTimers().length, 1, "再次打字排入新的去抖落盘");
  harness.document.visibilityState = "hidden";
  harness.dispatch("visibilitychange", {});
  assert.equal(harness.pendingTimers().length, 0, "显式 flush 必须清掉待触发的去抖定时器");
  assert.equal(harness.runTimers(), 0, "flush 之后没有可触发的去抖定时器");
  assert.equal(
    harness.fetches.filter(call => String(call.url).includes("/ui-state")).length, 2,
    "flush 之后不得再有第二次落盘（总量为定时器那次 + flush 那次）",
  );
});

test("页面隐藏与卸载流程各补一次落盘", () => {
  const harness = buildApp();
  type(harness, "隐藏前未落盘的草稿");
  harness.fetches.length = 0;
  harness.document.visibilityState = "hidden";
  harness.dispatch("visibilitychange", {});
  const hiddenFlush = harness.fetches.filter(call => String(call.url).includes("/ui-state"));
  assert.equal(hiddenFlush.length >= 1, true, "隐藏时补一次 ui-state 落盘");
  assert.match(String(hiddenFlush.at(-1).options.body), /隐藏前未落盘的草稿/, "落盘内容包含未发送内容");

  harness.fetches.length = 0;
  harness.dispatchWindow("pagehide", {});
  assert.equal(
    harness.fetches.filter(call => String(call.url).includes("/ui-state")).length >= 1, true,
    "卸载流程（pagehide）补一次 ui-state 落盘",
  );
});
