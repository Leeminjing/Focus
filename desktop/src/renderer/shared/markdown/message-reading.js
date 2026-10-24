/* Shared message reading for task conversations, robot answers and Context inspection.
 * Retains source text for copying/audit; caches bounded, width-independent Markdown HTML.
 * Hosts own navigation and input identity. Copy controls can be delegated via handleCopy.
 */
(function (root, factory) {
  const api = factory(typeof module === "object" && module.exports ? require("../../vendor/markdown-it.min.js") : root.markdownit);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusMessageReading = api;
})(typeof globalThis === "object" ? globalThis : this, function (markdownit) {
  "use strict";
  // 源文案在渲染时取当前语言；模板插值始终保留原文。
  function localize(source, ...values) {
    if (globalThis.FocusI18n?.text) return globalThis.FocusI18n.text(source, ...values);
    return typeof source === "string" ? source : source.reduce((out, part, i) => out + part + (i < values.length ? String(values[i]) : ""), "");
  }

  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const attribute = value => escape(value).replace(/\r/g, "&#13;").replace(/\n/g, "&#10;");
  function text(content) {
    if (typeof content === "string") return content;
    if (Array.isArray(content)) return content.filter(block => ["text", "output_text"].includes(block?.type) && typeof block.text === "string").map(block => block.text).join("\n");
    return content == null ? "" : JSON.stringify(content, null, 2);
  }
  function create({ translate = (_key, fallback) => fallback } = {}) {
    const markdown = markdownit({ html: false, linkify: true });
    markdown.validateLink = url => /^(https?:|file:)/i.test(url) || /^[a-z]:[\\/]/i.test(url)
      || (!/^[a-z][a-z\d+.-]*:/i.test(url) && !url.startsWith("//"));
    const cache = new Map();
    let cacheSize = 0;
    for (const rule of ["fence", "code_block"]) {
      const renderCode = markdown.renderer.rules[rule];
      markdown.renderer.rules[rule] = (tokens, index, options, env, renderer) => {
        const language = rule === "fence" ? tokens[index].info.trim().split(/\s+/)[0] : "";
        return `<div class="message-code-block"><div class="code-block-toolbar"><span>${escape(language)}</span><button type="button" class="text-button" data-action="copy-code" aria-live="polite">${escape(translate("conversation.copy_code", localize("复制代码")))}</button></div>${renderCode(tokens, index, options, env, renderer)}</div>\n`;
      };
    }
    function renderContent(value) {
      const source = String(value ?? "");
      const key = JSON.stringify([translate("conversation.copy_code", localize("复制代码")), source]);
      let html = cache.get(key);
      if (html !== undefined) { cache.delete(key); cache.set(key, html); }
      else {
        html = markdown.render(source);
        if (key.length + html.length <= 1024 * 1024) {
          cache.set(key, html); cacheSize += key.length + html.length;
          while (cache.size > 200 || cacheSize > 1024 * 1024) {
            const oldest = cache.keys().next().value;
            cacheSize -= oldest.length + cache.get(oldest).length; cache.delete(oldest);
          }
        }
      }
      return `<div class="message-rich">${html}</div>`;
    }
    function renderActions(source, label = translate("conversation.copy_answer", localize("复制回答"))) {
      return source.trim() ? `<div class="message-actions"><button type="button" class="text-button" data-action="copy-answer" data-copy-text="${attribute(source)}" aria-live="polite">${escape(label)}</button></div>` : "";
    }
    function toolRecord(label, source) {
      const preview = source.replace(/\s+/g, " ").trim().slice(0, 140) || localize("无正文");
      return `<details class="message-tool"><summary><strong>${escape(label)}</strong><span>${escape(preview)}</span></summary><pre>${escape(source)}</pre>${renderActions(source, localize("复制工具内容"))}</details>`;
    }
    function renderBody(message, source = text(message.content)) {
      const role = String(message.role || message.type || "").toLowerCase();
      const calls = message.tool_calls || message.additional_kwargs?.tool_calls || [];
      const tools = calls.map(call => toolRecord(localize`工具输入 · ${call.name || call.function?.name || "tool"}`, typeof call.function?.arguments === "string" ? call.function.arguments : JSON.stringify(call.args ?? call.function?.arguments ?? {}, null, 2))).join("");
      if (role === "tool" || role === "function") return toolRecord(localize`工具输出 · ${message.name || "tool"}`, source);
      const body = ["ai", "assistant"].includes(role) ? renderContent(source) : `<div class="message-plain">${escape(source)}</div>`;
      return body + tools;
    }
    function render(message, { source = text(message.content), copyLabel, provenance, revision, details = false } = {}) {
      const isTool = ["tool", "function"].includes(String(message.role || message.type || "").toLowerCase());
      const audit = details ? localize`<details class="message-audit"><summary>原始消息、来源与 Revision</summary>${revision ? `<h4>Revision</h4><pre>${escape(JSON.stringify(revision, null, 2))}</pre>` : ""}${provenance ? localize`<h4>来源${provenance.source_kind === "delegated_patrol" ? " · Patrol delegated" : ""}</h4><pre>${escape(JSON.stringify(provenance, null, 2))}</pre>` : ""}<h4>原始消息</h4><pre>${escape(JSON.stringify(message, null, 2))}</pre></details>` : "";
      return `<div class="message-reading">${renderBody(message, source)}${isTool ? "" : renderActions(source, copyLabel)}${audit}</div>`;
    }
    async function copyText(button, source, { onStatus = () => {} } = {}) {
      if (button.disabled || typeof source !== "string") return;
      const label = button.textContent;
      button.disabled = true;
      try {
        await navigator.clipboard.writeText(source);
        button.textContent = translate("conversation.copied", localize("已复制")); onStatus(button.textContent, false);
      } catch {
        button.textContent = translate("conversation.copy_failed", localize("复制失败，请重试")); onStatus(button.textContent, true);
      } finally {
        setTimeout(() => { button.textContent = label; button.disabled = false; }, 1600);
      }
    }
    function handleCopy(event, options) {
      const button = event.target.closest("[data-action]");
      if (!["copy-answer", "copy-code"].includes(button?.dataset.action)) return false;
      event.preventDefault();
      void copyText(button, button.dataset.action === "copy-code" ? button.closest(".message-code-block")?.querySelector("pre > code")?.textContent : button.dataset.copyText, options);
      return true;
    }
    return Object.freeze({ markdown, renderContent, renderBody, renderActions, render, copyText, handleCopy });
  }
  return Object.freeze({ create, text, ...create() });
});
