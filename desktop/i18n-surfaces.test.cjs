const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const { createElement } = require("./test-dom.cjs");

function surface(locale = "en-US") {
  const document = { documentElement: { dataset: {} }, dispatchEvent() {}, querySelectorAll: () => [] };
  const context = vm.createContext({ document, localStorage: { getItem: () => locale, setItem() {} }, CustomEvent: class {} });
  context.window = context;
  for (const file of ["i18n-catalog.js", "i18n.js", "plugin-view.js", "memory.js", "session-controls.js", "workspace-patrol-feedback.js", "patrol-input-references.js", "workspace-patrol-view.js", "loop-expansion-budget.js", "loop-facts-view.js", "loop-mission-editor.js", "workspace-patrol-budget.js"]) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, file), "utf8"), context, { filename: file });
  }
  return context;
}

for (const file of ["index.html", "desktop-patrol.html", "splash.html"]) test(`English ${file} translates text, titles and accessible labels`, () => {
  const context = surface();
  const root = createElement("div");
  root.innerHTML = fs.readFileSync(path.join(__dirname, file), "utf8").replace(/<!--[\s\S]*?-->/g, "");
  context.FocusI18n.apply(root);
  const untranslated = [];
  function visit(node) {
    if (["script", "style"].includes(node.tagName)) return;
    if (node.nodeType === 3 && /\p{Script=Han}/u.test(node.textContent)) untranslated.push(node.textContent.trim());
    for (const name of ["aria-label", "title", "placeholder"]) {
      if (/\p{Script=Han}/u.test(node.getAttribute?.(name) || "")) untranslated.push(node.getAttribute(name));
    }
    for (const child of node.childNodes || []) visit(child);
  }
  visit(root);
  assert.deepEqual(untranslated, []);
});

test("English catalog has a translation for every source message", () => {
  const catalog = require("./i18n-catalog.js");
  for (const [source, english] of Object.entries(catalog)) {
    assert.equal(typeof english, "string", source);
    assert.doesNotMatch(english, /\p{Script=Han}/u, source);
  }
});

test("source templates translate only literals and preserve interpolation values", () => {
  const c = surface();
  const value = "刷新 <script>用户内容</script>";
  assert.equal(c.FocusI18n.text`文件 / 产物${value}`, `Files / Outputs${value}`);
  assert.equal(c.FocusI18n.text`上下文约${95}% 已用`, "Context about 95% used");
  c.FocusI18n.setLocale("zh-CN");
  assert.equal(c.FocusI18n.text`上下文约${95}% 已用`, "上下文约95% 已用");
});

test("English dynamic views contain no built-in Chinese text", () => {
  const c = surface();
  const escape = value => String(value ?? "");
  const html = [
    c.FocusPluginView.render([], {}, [], {}),
    c.FocusMemoryView.render([], {}),
    c.FocusSessionControls.render({ models: [], default_model_name: "" }, {}, null, escape),
    c.FocusWorkspacePatrolView.skeleton({ workspace_id: "w", display_name: "Workspace" }),
  ].join("\n");
  assert.doesNotMatch(html, /\p{Script=Han}/u);
});

test("language changes refresh generated labels and preserve user content", () => {
  const c = surface("zh-CN");
  const render = () => c.FocusPluginView.render([{ name: "用户插件", version: "1", status: "active", requires: [] }], {}, [], {});
  assert.match(render(), /已注册/);
  c.FocusI18n.setLocale("en-US");
  const english = render();
  assert.match(english, /用户插件/);
  assert.doesNotMatch(english.replaceAll("用户插件", ""), /\p{Script=Han}/u);
  c.FocusI18n.setLocale("zh-CN");
  assert.match(render(), /已注册/);
});

test("budget and fact labels follow repeated language switches after modules have loaded", () => {
  const c = surface("zh-CN");
  const render = () => [
    c.FocusLoopExpansionBudget.renderLoopInputs(),
    c.FocusLoopMissionEditor.render({}),
    c.FocusLoopFactsView.statusOptions.map(row => row[1]).join(" "),
    c.FocusLoopFactsView.outcomeOptions.map(row => row[1]).join(" "),
    c.FocusWorkspacePatrolBudget.markup(),
  ].join("\n");
  assert.match(render(), /最大轮次/);
  c.FocusI18n.setLocale("en-US");
  assert.doesNotMatch(render(), /\p{Script=Han}/u);
  c.FocusI18n.setLocale("zh-CN");
  assert.match(render(), /最大轮次/);
});

test("native chrome follows the saved language and leaves unknown content intact", () => {
  const { createNativeI18n } = require("./native-i18n.cjs");
  let locale = "zh-CN";
  const text = createNativeI18n(() => locale);
  assert.equal(text("退出 Focus"), "退出 Focus");
  locale = "en-US";
  assert.equal(text("退出 Focus"), "Quit Focus");
  assert.equal(text("我的项目"), "我的项目");
});
