/*
 * 本文件对外提供 F21 流动密度静态守卫。输入为现有语义 CSS 与 legacy 样式，输出为内容驱动
 * 用户气泡、紧凑会话轨迹、轻量表面和方案 B 样式收敛断言；工作流只读源码并在视觉架构回退时失败。
 * 示例：`node f21-fluid-ui.test.cjs`。
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const read = relative => fs.readFileSync(path.join(root, relative), "utf8");
const rule = (source, selector) => {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return source.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`))?.[1] || "";
};
const rules = (source, selector) => {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return [...source.matchAll(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`, "g"))].map(match => match[1]);
};
const exactRules = (source, selector) => {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return [...source.matchAll(new RegExp(`(?:^|[{}])\\s*${escaped}\\s*\\{([^}]*)\\}`, "gm"))].map(match => match[1]);
};

const tokens = read("desktop/styles/tokens.css");
const components = read("desktop/styles/components.css");
const shell = read("desktop/styles/shell.css");
const views = read("desktop/styles/views.css");
const events = read("desktop/styles/conversation-events.css");
const legacy = read("desktop/styles.css");

const human = rule(views, ".work-record.human");
assert.match(human, /inline-size:\s*fit-content/);
assert.match(human, /max-inline-size:/);
assert.doesNotMatch(human, /width:\s*min\(78%/);

const event = rule(events, ".conversation-event");
const summary = rule(events, ".conversation-event > summary");
assert.match(event, /width:\s*fit-content/);
assert.match(event, /max-width:/);
assert.match(summary, /display:\s*inline-grid/);
assert.doesNotMatch(summary, /minmax\(0,\s*1fr\)/);
assert.match(summary, /min-height:\s*(24|28)px/);

assert.match(tokens, /--shell-header-height:\s*(52|56)px/);
assert.match(tokens, /--shell-nav-width:\s*(164|172)px/);
assert.match(rule(components, ".ui-card"), /border:\s*0/);
assert.match(rule(views, ".focus-view"), /border:\s*0/);
assert.ok(rules(shell, ".app-inspector").some(body => /box-shadow:\s*none/.test(body)));

for (const selector of [".app-header", ".brand", "#app", ".focus-shell", ".focus-view", ".conversation", ".message.human", ".composer", ".task-grid", ".task-card", ".draft-panel"]) {
  assert.equal(exactRules(legacy, selector).length, 0, `${selector} 的冲突 legacy 规则必须迁移或删除`);
}

assert.doesNotMatch(views + shell + events, /field-sizing\s*:|scrollHeight|ResizeObserver/);

console.log("f21-fluid-ui: 内容驱动气泡、紧凑轨迹、轻量表面与方案 B 收敛守卫通过");
