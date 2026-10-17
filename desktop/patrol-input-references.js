/* 两端 Patrol 输入历史共用的版本引用与附件预览。
 * 输入冻结的历史行和 intake；输出紧凑文件名/摘要/缩略图及按需展开的原版本。
 * 字节仅经认证请求读取；Blob URL 在工作区切换、历史移除和卸载时释放。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.FocusPatrolInputReferences = api;
})(globalThis, function () {
  "use strict";
  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const name = item => String(item.name || item.relative_path || item.path || "附件").split(/[\\/]/).at(-1);
  const digest = item => item.sha256 || item.digest || item.result_key || "";
  const image = item => (item.material_kind || item.source_kind) === "image";
  function render(row) {
    const attachments = (row.attachments || []).map((item, index) => `<details data-patrol-history-attachment="${index}"><summary>${image(item) ? `<img alt="${escape(name(item))}" loading="lazy">` : ""}<span>${escape(name(item))}<small>${digest(item) ? `版本 ${escape(digest(item).slice(0, 8))}` : "待上传版本"}</small></span></summary><div data-patrol-history-preview></div></details>`).join("");
    const files = (row.file_refs || []).map((file, index) => `<button type="button" data-patrol-history-file="${index}" title="${escape(file.path)}"><span>${escape(name(file))}<small>版本 ${escape(file.revision)} · ${escape(digest(file).slice(0, 8))}</small></span></button>`).join("");
    return attachments || files ? `<div class="patrol-history-references" aria-label="本次输入的附件和文件版本">${attachments}${files}</div>` : "";
  }
  function create({ host, intake, workspaceId, onFile, onError }) {
    const lifetime = new AbortController(), entries = new Map(), attempts = new WeakMap();
    let reads = new AbortController(), owner = workspaceId(), rows = new Map();
    const key = (row, item) => JSON.stringify([owner, row.intent_id || row.submission_id, item.material_id || item.local_id, digest(item)]);
    function release(id) { const entry = entries.get(id); entries.delete(id); if (entry?.url) URL.revokeObjectURL(entry.url); }
    function reset() { reads.abort(); reads = new AbortController(); for (const id of entries.keys()) release(id); }
    function identify(node) {
      const row = rows.get(node.closest("[data-submission-id]")?.dataset.submissionId);
      return { row, item: row?.attachments?.[Number(node.dataset.patrolHistoryAttachment)] };
    }
    function currentVersion(node, id) {
      const { row, item } = identify(node);
      return node.isConnected && row && item && key(row, item) === id;
    }
    function load(row, item) {
      const id = key(row, item);
      if (entries.has(id)) return entries.get(id).promise;
      const entry = {};
      entry.promise = intake.attachmentContent(owner, row, item, reads.signal).then(blob => {
        if (entries.get(id) !== entry) throw new Error("附件预览已关闭");
        entry.url = URL.createObjectURL(blob);
        return { blob, url: entry.url };
      }).catch(error => { if (entries.get(id) === entry) release(id); throw error; });
      entries.set(id, entry);
      return entry.promise;
    }
    async function thumbnail(node) {
      const { row, item } = identify(node);
      const img = node.querySelector("summary img");
      if (!row || !item || !img || img.hasAttribute("src")) return;
      const id = key(row, item);
      try {
        const { url } = await load(row, item);
        if (currentVersion(node, id)) img.src = url;
      } catch { if (img.isConnected) img.title = "提交版本暂时无法预览，可展开后重试"; }
    }
    const observer = typeof IntersectionObserver === "function" ? new IntersectionObserver(items => {
      for (const entry of items) if (entry.isIntersecting) { observer.unobserve(entry.target); void thumbnail(entry.target); }
    }) : null;
    host.addEventListener("toggle", async event => {
      const node = event.target;
      if (!node.matches("[data-patrol-history-attachment]") || !node.open) return;
      const { row, item } = identify(node), body = node.querySelector("[data-patrol-history-preview]");
      if (!row || !item || body.childElementCount) return;
      const attempt = {};
      attempts.set(body, attempt);
      const id = key(row, item);
      const current = () => currentVersion(node, id) && attempts.get(body) === attempt;
      body.textContent = "正在读取提交版本…";
      try {
        const { blob, url } = await load(row, item);
        if (!current()) return;
        body.replaceChildren();
        if (image(item)) { const img = document.createElement("img"); img.src = url; img.alt = name(item); body.append(img); }
        else if (item.material_kind === "text" || item.source_kind === "text" || blob.type.startsWith("text/") || /\.(txt|md|json|csv|log|js|ts|py|yaml|yml|html|css)$/i.test(name(item))) {
          const text = await blob.slice(0, 65536).text();
          if (!current()) return;
          const pre = document.createElement("pre"); pre.textContent = text + (blob.size > 65536 ? "\n…预览已截断，请保存完整文件查看" : ""); body.append(pre);
        }
        const link = document.createElement("a"); link.href = url; link.download = name(item); link.textContent = `保存 ${name(item)} · ${blob.size} 字节`; body.append(link);
        const version = document.createElement("small"); version.textContent = digest(item) ? `SHA-256 ${digest(item)}` : "本次输入的本地附件"; body.append(version);
      } catch (error) { if (current()) body.textContent = `提交版本不可用：${error.message}。收起后展开可重试。`; }
    }, { capture: true, signal: lifetime.signal });
    host.addEventListener("click", event => {
      const button = event.target.closest("[data-patrol-history-file]");
      if (!button) return;
      const row = rows.get(button.closest("[data-submission-id]")?.dataset.submissionId);
      const file = row?.file_refs?.[Number(button.dataset.patrolHistoryFile)];
      if (file) { event.preventDefault(); void Promise.resolve().then(() => onFile(row, file)).catch(onError); }
    }, { signal: lifetime.signal });
    return Object.freeze({
      bind(model) {
        if (owner !== workspaceId()) { reset(); owner = workspaceId(); }
        rows = new Map(model.rows.map(row => [row.submission_id, row]));
        const used = new Set(model.rows.flatMap(row => (row.attachments || []).map(item => key(row, item))));
        for (const id of entries.keys()) if (!used.has(id)) release(id);
        observer?.disconnect();
        for (const node of host.querySelectorAll("[data-patrol-history-attachment]")) {
          if (observer) observer.observe(node); else void thumbnail(node);
        }
      },
      dispose() { lifetime.abort(); observer?.disconnect(); reset(); rows.clear(); },
    });
  }
  return Object.freeze({ render, create });
});
