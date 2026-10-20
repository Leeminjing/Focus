/* 本文件对外提供 FocusWorkspacePatrolMaterialPreview.readTransfer 与 create。
 * 输入为浏览器拖放/剪贴板、服务端材料上限和共享 intake；输出为有界字节包与可增删的材料卡。
 * 工作流为先检查数量、文件夹和大小，再读取 File 字节；草稿仍由既有 store 持有，图片 URL 随卡片移除释放。
 * 要求输入框内的纯文本粘贴保留原生行为；外部拖入文字成为参考材料。示例：preview.render(intake.store(id).get().draft)。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.FocusWorkspacePatrolMaterialPreview = api;
})(globalThis, function () {
  "use strict";
  // 源文案在渲染时取当前语言；模板插值始终保留原文。
  function localize(source, ...values) {
    if (globalThis.FocusI18n?.text) return globalThis.FocusI18n.text(source, ...values);
    return typeof source === "string" ? source : source.reduce((out, part, i) => out + part + (i < values.length ? String(values[i]) : ""), "");
  }

  function captureTransfer(data) {
    if ([...(data?.items || [])].some(item => item.webkitGetAsEntry?.()?.isDirectory)) throw new Error(localize("文件夹请用“绑定工作区”选择；这里只接收文件和文字"));
    const text = data?.getData("text/plain") || "";
    return { files: [...(data?.files || [])], getData: () => text };
  }
  async function readTransfer(data, limits) {
    const items = [...(data?.items || [])];
    if (items.some(item => item.webkitGetAsEntry?.()?.isDirectory)) throw new Error(localize("文件夹请用“绑定工作区”选择；这里只接收文件和文字"));
    const files = [...(data?.files || [])];
    if (files.length > Math.min(32, limits.run_material_count)) throw new Error(localize("材料数量超限"));
    let total = 0;
    for (const file of files) {
      total += file.size;
      if (!file.size || file.size > (file.type.startsWith("image/") ? limits.original_image_bytes : limits.general_file_bytes) || total > limits.general_file_bytes) throw new Error(localize("材料为空或超过大小限制"));
    }
    if (files.length) return Promise.all(files.map(async file => ({ kind: "file", name: file.name || localize("截图.png"), type: file.type, bytes: new Uint8Array(await file.arrayBuffer()) })));
    const text = data?.getData("text/plain") || "";
    if (new TextEncoder().encode(text).byteLength > limits.general_file_bytes) throw new Error(localize("参考文字超过大小限制"));
    return text.trim() ? [{ kind: "text", text }] : [];
  }
  function create({ host, intake, workspaceId, onError, compact = false }) {
    const lifetime = new AbortController(), urls = new Map();
    const options = { signal: lifetime.signal };
    let queue = Promise.resolve(), rendered = "";
    function receive(event) {
      const data = event.dataTransfer || event.clipboardData;
      if (!data) return;
      if (event.type === "paste" && !data.files?.length && event.target.closest("textarea, input, [contenteditable=true]")) return;
      event.preventDefault();
      host.classList.remove("is-dragging");
      const id = workspaceId();
      let captured;
      try { captured = captureTransfer(data); } catch (error) { onError(error); return; }
      const transfer = intake.resourceLimits().then(limits => readTransfer(captured, limits));
      queue = queue.then(async () => { const items = await transfer; if (items.length) await intake.add(id, items); }).catch(onError);
    }
    host.addEventListener("dragover", event => { event.preventDefault(); host.classList.add("is-dragging"); }, options);
    host.addEventListener("dragleave", event => { if (!host.contains(event.relatedTarget)) host.classList.remove("is-dragging"); }, options);
    host.addEventListener("drop", receive, options);
    host.addEventListener("paste", receive, options);
    host.addEventListener("click", event => {
      const button = event.target.closest("[data-material-remove]");
      if (button) { event.stopPropagation(); void intake.remove(workspaceId(), button.dataset.materialRemove).catch(onError); }
    }, options);
    function render(draft) {
      const signature = JSON.stringify(draft.attachments || []);
      if (signature === rendered) return;
      rendered = signature;
      const cards = draft.attachments || [], active = new Set(cards.map(item => item.local_id));
      for (const [id, url] of urls) if (!active.has(id)) { URL.revokeObjectURL(url); urls.delete(id); }
      const container = host.querySelector("[data-patrol-materials]");
      if (!container) return;
      const nodes = cards.map(item => {
        const card = document.createElement("article");
        card.className = "patrol-material-card";
        card.dataset.materialKind = item.source_kind;
        const bytes = intake.source(item.local_id);
        if (item.source_kind === "image" && bytes) {
          if (!urls.has(item.local_id)) urls.set(item.local_id, URL.createObjectURL(new Blob([bytes], { type: item.type })));
          const img = document.createElement("img"); img.src = urls.get(item.local_id); img.alt = item.name; card.append(img);
        }
        const label = document.createElement("span"); label.textContent = compact && item.source_kind === "text" ? localize("参考文字") : item.name; label.title = item.name; card.append(label);
        if (item.source_kind === "text" && bytes) {
          const text = document.createElement("pre"); text.textContent = new TextDecoder().decode(bytes.slice(0, 1200)); card.append(text);
        }
        const size = document.createElement("small"); size.textContent = compact ? (item.source_kind === "image" ? localize("图片") : localize("参考材料")) : localize`${Math.max(1, Math.ceil(item.size / 1024))} KB · 参考材料`; card.append(size);
        const remove = document.createElement("button"); remove.type = "button"; remove.dataset.materialRemove = item.local_id; remove.textContent = localize("移除"); remove.setAttribute("aria-label", localize`移除 ${item.name}`); card.append(remove);
        return card;
      });
      container.replaceChildren(...nodes);
    }
    return Object.freeze({ render, dispose() { lifetime.abort(); for (const url of urls.values()) URL.revokeObjectURL(url); urls.clear(); } });
  }
  return Object.freeze({ captureTransfer, readTransfer, create });
});
