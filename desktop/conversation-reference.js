/* Ordinary conversation quotations. Offsets count UTF-16 code units in rendered
 * message-content.textContent with code toolbars removed, never raw Markdown.
 * Drafts belong to a task; consumption compares the submitted snapshot so a
 * quotation added during an upload/request remains available for the next turn.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusConversationReference = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";
  // 源文案在渲染时取当前语言；模板插值始终保留原文。
  function localize(source, ...values) {
    if (globalThis.FocusI18n?.text) return globalThis.FocusI18n.text(source, ...values);
    return typeof source === "string" ? source : source.reduce((out, part, i) => out + part + (i < values.length ? String(values[i]) : ""), "");
  }

  const escape = value => String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
  const clone = value => value ? JSON.parse(JSON.stringify(value)) : null;
  function createDrafts(storage) {
    const drafts = new Map();
    const key = taskId => `focus-conversation-reference:${taskId}`;
    function get(taskId, fallback = null) {
      if (!drafts.has(taskId)) {
        let saved;
        try { saved = JSON.parse(storage?.getItem(key(taskId)) || "null"); } catch { /* use server draft */ }
        drafts.set(taskId, saved || fallback);
      }
      return clone(drafts.get(taskId));
    }
    function set(taskId, reference) {
      drafts.set(taskId, clone(reference));
      // A null tombstone also suppresses an older server draft after reload.
      try { storage?.setItem(key(taskId), JSON.stringify({ reference: clone(reference) })); } catch { /* server persistence remains available */ }
    }
    return {
      get(taskId, fallback = null) {
        const saved = get(taskId, fallback);
        return saved && Object.hasOwn(saved, "reference") ? saved.reference : saved;
      },
      set,
      consume(taskId, submitted) {
        if (JSON.stringify(this.get(taskId)) === JSON.stringify(submitted)) set(taskId, null);
      },
    };
  }
  function fragmentText(fragment) {
    fragment.querySelectorAll(".code-block-toolbar").forEach(node => node.remove());
    return fragment.textContent;
  }
  function capture(selection, conversation, taskId) {
    if (!selection || selection.isCollapsed || selection.rangeCount !== 1 || !conversation) return null;
    const range = selection.getRangeAt(0);
    const element = node => node.nodeType === 1 ? node : node.parentElement;
    const startNode = element(range.startContainer), endNode = element(range.endContainer);
    const content = startNode?.closest(".message-content");
    const article = content?.closest(".message.ai[data-message-id]");
    if (!article || !conversation.contains(article) || !content.contains(endNode)
        || startNode.closest(".code-block-toolbar") || endNode.closest(".code-block-toolbar")) return null;
    const prefix = range.cloneRange();
    prefix.selectNodeContents(content);
    prefix.setEnd(range.startContainer, range.startOffset);
    const start = fragmentText(prefix.cloneContents());
    const text = fragmentText(range.cloneContents());
    if (!text.trim() || text.length > 16000) return null;
    return { task_id: taskId, message_id: article.dataset.messageId, text,
      start: start.length, end: start.length + text.length, offset_basis: "rendered_text_utf16" };
  }
  function render(reference, { editable = true } = {}) {
    if (!reference) return "";
    return localize`<aside class="conversation-reference" aria-label="引用原文"><div><button type="button" class="text-button" data-action="reference-source" data-reference="${escape(JSON.stringify(reference))}">引用回答 · 跳回原消息</button>${editable ? localize('<button type="button" class="text-button" data-action="reference-remove" aria-label="移除引用">移除</button>') : ""}</div><blockquote>${escape(reference.text)}</blockquote></aside>`;
  }
  function stripProtocol(value, reference) {
    if (!reference || typeof value !== "string" || !value.endsWith("\n</focus_message_reference>")) return value;
    const start = value.lastIndexOf("<focus_message_reference>\n");
    if (start === 0) return "";
    return start > 1 && value.slice(start - 2, start) === "\n\n" ? value.slice(0, start - 2) : value;
  }
  function selectRange(content, reference, window) {
    const walker = content.ownerDocument.createTreeWalker(content, 4);
    const nodes = [];
    let node;
    while ((node = walker.nextNode())) if (!node.parentElement?.closest(".code-block-toolbar")) nodes.push(node);
    const text = nodes.map(node => node.textContent).join("");
    if (text.slice(reference.start, reference.end) !== reference.text) return false;
    let offset = 0, start = null, end = null;
    for (const node of nodes) {
      const next = offset + node.length;
      if (!start && reference.start >= offset && reference.start <= next) start = [node, reference.start - offset];
      if (!end && reference.end >= offset && reference.end <= next) end = [node, reference.end - offset];
      offset = next;
    }
    if (!start || !end) return false;
    const range = content.ownerDocument.createRange();
    range.setStart(...start); range.setEnd(...end);
    const selection = window.getSelection();
    selection.removeAllRanges(); selection.addRange(range);
    return true;
  }
  function mount({ document, window, getTaskId, onQuote, onStatus }) {
    let toolbar = null, pending = null;
    const hide = () => { toolbar?.remove(); toolbar = null; pending = null; };
    function update() {
      if (toolbar?.contains(document.activeElement)) return;
      const taskId = getTaskId();
      const selection = window.getSelection();
      const reference = taskId && capture(selection, document.querySelector("#conversation"), taskId);
      if (!reference) return hide();
      hide();
      pending = reference;
      toolbar = document.createElement("div");
      toolbar.className = "conversation-selection-toolbar";
      toolbar.setAttribute("role", "toolbar");
      toolbar.setAttribute("aria-label", localize("选中文字操作"));
      toolbar.innerHTML = localize('<button type="button" data-reference-command="quote">追问</button><button type="button" data-reference-command="copy">复制</button>');
      // Keep the range alive when pressing the floating controls with a pointer.
      toolbar.addEventListener("pointerdown", event => event.preventDefault());
      toolbar.addEventListener("click", async event => {
        const command = event.target.closest("[data-reference-command]")?.dataset.referenceCommand;
        const submitted = pending;
        if (!command || !submitted || submitted.task_id !== getTaskId()) return hide();
        if (command === "quote") {
          onQuote(submitted);
          hide(); selection.removeAllRanges();
        } else {
          try { await window.navigator.clipboard.writeText(submitted.text); onStatus(localize("已复制")); }
          catch { onStatus(localize("复制失败，请重试"), true); }
          hide();
        }
      });
      document.body.append(toolbar);
      const rect = selection.getRangeAt(0).getBoundingClientRect();
      toolbar.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - toolbar.offsetWidth - 8))}px`;
      toolbar.style.top = `${Math.max(8, Math.min(rect.top - toolbar.offsetHeight - 8, window.innerHeight - toolbar.offsetHeight - 8))}px`;
    }
    document.addEventListener("selectionchange", update);
    document.addEventListener("keydown", event => { if (event.key === "Escape") hide(); });
    document.addEventListener("pointerdown", event => { if (!toolbar?.contains(event.target)) hide(); });
    document.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
    return { hide };
  }
  return { createDrafts, capture, render, stripProtocol, selectRange, mount };
});
