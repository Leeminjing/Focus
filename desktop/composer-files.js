/* Workspace mentions and line quotations for the ordinary conversation composer. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.FocusComposerFiles = api;
})(globalThis, function () {
  "use strict";
  // 源文案在渲染时取当前语言；模板插值始终保留原文。
  function localize(source, ...values) {
    if (globalThis.FocusI18n?.text) return globalThis.FocusI18n.text(source, ...values);
    return typeof source === "string" ? source : source.reduce((out, part, i) => out + part + (i < values.length ? String(values[i]) : ""), "");
  }

  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  function mention(value, caret = value.length) {
    const before = value.slice(0, caret), match = before.match(/(?:^|[\s(])@([^\s@]*)$/);
    if (!match || match[1].startsWith("关键字快捷压缩")) return null;
    return { query: match[1], start: before.length - match[1].length - 1, end: caret };
  }
  function key(ref) {
    return JSON.stringify([ref.workspace_id, ref.path, ref.slot_id, ref.run_id, ref.view || "current",
      ref.fact_id, ref.revision, ref.result_key, ref.sha256, ref.version_id, ref.start_line, ref.end_line, ref.text]);
  }
  function label(ref) {
    return `${ref.path.split(/[\\/]/).pop()}${ref.start_line ? localize` · 第 ${ref.start_line}–${ref.end_line || ref.start_line} 行` : ""}`;
  }
  function cards(refs) {
    return refs.map((ref, index) => localize`<span class="composer-file-card" title="${escape(ref.path)}"><button type="button" data-action="open-composer-file" data-reference-index="${index}">${escape(label(ref))}</button><button type="button" data-action="remove-composer-file" data-reference-index="${index}" aria-label="移除 ${escape(label(ref))}">×</button></span>`).join("");
  }
  function compose(message, refs) {
    if (!refs.length) return message;
    const blocks = refs.map(ref => {
      const href = ref.path.replace(/\\/g, "/").split("/").map(encodeURIComponent).join("/")
        + (ref.start_line ? `#L${ref.start_line}-L${ref.end_line || ref.start_line}` : "");
      let block = localize`引用文件：[${label(ref).replace(/[\\[\]]/g, "\\$&")}](${href})`;
      if (typeof ref.text === "string") {
        const fence = "`".repeat(Array.from(ref.text.matchAll(/`+/g), match => match[0].length + 1).reduce((max, length) => Math.max(max, length), 3));
        block += localize`\n以下是引用时的文件内容：\n${fence}\n${ref.text}\n${fence}`;
      }
      return block;
    });
    return `${blocks.join("\n\n")}\n\n${message}`.trim();
  }
  function bind({ input, menu, search, onSelect, onError = () => {} }) {
    let serial = 0, timer, results = [], active = 0, current = null, closed = false;
    const previousControls = input.getAttribute("aria-controls");
    function close() {
      serial++; clearTimeout(timer); results = []; current = null; menu.hidden = true;
      if (input.getAttribute("aria-controls") === menu.id) {
        if (previousControls) input.setAttribute("aria-controls", previousControls);
        else input.removeAttribute("aria-controls");
        input.setAttribute("aria-expanded", "false"); input.removeAttribute("aria-activedescendant");
      }
    }
    function paint() {
      menu.hidden = false;
      menu.innerHTML = results.length ? results.map((ref, i) => `<button type="button" id="fileMention${i}" role="option" aria-selected="${i === active}" class="file-mention-option${i === active ? " is-active" : ""}" data-file-index="${i}">${escape(ref.relative_path || ref.path)}</button>`).join("") : localize('<div role="status">没有匹配文件</div>');
      input.setAttribute("aria-controls", menu.id);
      input.setAttribute("aria-expanded", "true");
      if (results.length) {
        input.setAttribute("aria-activedescendant", `fileMention${active}`);
        menu.querySelector(".is-active")?.scrollIntoView?.({ block: "nearest" });
      } else input.removeAttribute("aria-activedescendant");
    }
    function update() {
      close();
      if (closed || input.disabled) return;
      current = mention(input.value, input.selectionStart ?? input.value.length);
      if (!current) return;
      const request = ++serial, token = current;
      menu.hidden = false; menu.innerHTML = localize('<div role="status">正在搜索工作区文件…</div>');
      input.setAttribute("aria-controls", menu.id); input.setAttribute("aria-expanded", "true");
      timer = setTimeout(async () => {
        try {
          const response = await search(token.query);
          if (closed || serial !== request) return;
          results = response.files || []; active = 0; paint();
        } catch (error) {
          if (closed || serial !== request) return;
          menu.innerHTML = localize`<div role="alert">文件搜索失败：${escape(error.message)}</div>`;
          onError(error);
        }
      }, 120);
      return true;
    }
    function select(index) {
      const ref = results[index], token = current;
      if (!ref || !token) return;
      const now = mention(input.value, input.selectionStart ?? input.value.length);
      if (!now || now.start !== token.start || now.end !== token.end || now.query !== token.query) return close();
      onSelect(ref);
      input.value = input.value.slice(0, token.start) + input.value.slice(token.end);
      close(); input.focus(); input.setSelectionRange(token.start, token.start);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
    function keydown(event) {
      if (menu.hidden || !current || event.isComposing || event.keyCode === 229) return false;
      if (event.shiftKey || event.altKey || event.ctrlKey || event.metaKey) return false;
      if (!["ArrowDown", "ArrowUp", "Enter", "Tab", "Escape"].includes(event.key)) return false;
      if (event.key === "Tab" && !results.length) { close(); return false; }
      event.preventDefault(); event.stopPropagation();
      if (event.key === "Escape") close();
      else if (event.key === "Enter" || event.key === "Tab") select(active);
      else if (results.length) { active = (active + (event.key === "ArrowDown" ? 1 : -1) + results.length) % results.length; paint(); }
      return true;
    }
    const pointerdown = event => { if (event.target.closest?.("[data-file-index]")) event.preventDefault(); };
    const click = event => { const button = event.target.closest?.("[data-file-index]"); if (button) select(Number(button.dataset.fileIndex)); };
    const blur = () => close();
    input.addEventListener("keydown", keydown);
    input.addEventListener("click", update);
    input.addEventListener("blur", blur);
    menu.addEventListener("pointerdown", pointerdown); menu.addEventListener("click", click);
    return { update, close, destroy() {
      closed = true; close(); input.removeEventListener("keydown", keydown); input.removeEventListener("click", update);
      input.removeEventListener("blur", blur); menu.removeEventListener("pointerdown", pointerdown); menu.removeEventListener("click", click);
    } };
  }
  return { mention, key, label, cards, compose, bind };
});
