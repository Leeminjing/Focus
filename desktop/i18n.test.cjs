const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function loadI18n(initial = {}) {
  const values = new Map(Object.entries(initial));
  const document = {
    documentElement: { lang: "", dataset: {} },
    dispatchEvent() {},
  };
  const window = {
    localStorage: {
      getItem(key) { return values.get(key) ?? null; },
      setItem(key, value) { values.set(key, String(value)); },
    },
    document,
    CustomEvent: class CustomEvent {
      constructor(type, options) { this.type = type; this.detail = options?.detail; }
    },
  };
  window.window = window;
  const source = fs.readFileSync(path.join(__dirname, "i18n.js"), "utf8");
  vm.runInNewContext(source, window, { filename: "i18n.js" });
  return { i18n: window.FocusI18n, values, document };
}

test("interface language defaults to Chinese and translates known labels", () => {
  const { i18n, document } = loadI18n();

  assert.equal(i18n.locale(), "zh-CN");
  assert.equal(i18n.t("nav.tasks"), "任务");
  assert.equal(document.documentElement.lang, "zh-CN");
});

test("user can switch to English and the choice survives reload", () => {
  const first = loadI18n();
  first.i18n.setLocale("en-US");

  assert.equal(first.i18n.t("nav.tasks"), "Task");
  assert.equal(first.values.get("focus-interface-language-v1"), "en-US");

  const second = loadI18n({
    "focus-interface-language-v1": first.values.get("focus-interface-language-v1"),
  });
  assert.equal(second.i18n.locale(), "en-US");
  assert.equal(second.i18n.t("settings.archived_count", { count: 3 }), "3 archived sessions");
});

test("unsupported stored language falls back to Chinese", () => {
  const { i18n } = loadI18n({ "focus-interface-language-v1": "fr-FR" });
  assert.equal(i18n.locale(), "zh-CN");
});

test("translation applies to declarative text and accessible labels", () => {
  const { i18n } = loadI18n({ "focus-interface-language-v1": "en-US" });
  const textNode = { dataset: { i18n: "nav.tasks" }, textContent: "" };
  const ariaNode = {
    dataset: { i18nAriaLabel: "nav.settings" },
    setAttribute(name, value) { this[name] = value; },
  };
  const root = {
    querySelectorAll(selector) {
      if (selector === "[data-i18n]") return [textNode];
      if (selector === "[data-i18n-aria-label]") return [ariaNode];
      return [];
    },
  };

  i18n.apply(root);

  assert.equal(textNode.textContent, "Task");
  assert.equal(ariaNode["aria-label"], "Settings");
});

test("settings exposes both languages and loads localization before the application", () => {
  const index = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
  assert.match(index, /data-action="set-language" data-locale="zh-CN"/);
  assert.match(index, /data-action="set-language" data-locale="en-US"/);
  assert.ok(index.indexOf("./i18n.js") < index.indexOf("./app.js"));
});
