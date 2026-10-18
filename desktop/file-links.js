/* Shared local-link identity for the workbench and robot. Paths come only from
 * href and are decoded once; the backend validates the owning task/run/slot. */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  root.FocusFileLinks = api;
})(globalThis, function (root) {
  "use strict";
  function pathFromHref(href) {
    if (!href || href.startsWith("#") || /^https?:/i.test(href)) return null;
    if (/^[a-z][a-z\d+.-]*:/i.test(href) && !/^file:/i.test(href) && !/^[a-z]:[\\/]/i.test(href)) return null;
    let path = href.replace(/[?#].*$/, "");
    if (/^file:/i.test(path)) {
      path = path.replace(/^file:\/\/(localhost)?(?=\/)/i, "").replace(/^file:/i, "");
      if (/^\/[a-z]:[\\/]/i.test(path)) path = path.slice(1);
      else if (!path.startsWith("/")) path = `//${path}`;
    }
    return decodeURIComponent(path).replace(/\\/g, "/");
  }
  function reference(value = {}) {
    const ref = {};
    for (const key of ["path", "workspace_id", "run_id", "slot_id", "view", "fact_id", "revision", "result_key", "sha256", "version_id"])
      if (value[key] != null && value[key] !== "") ref[key] = value[key];
    if (["fact_id", "revision", "result_key", "sha256", "version_id"].some(key => ref[key] != null)) ref.view = "history";
    ref.view ||= "current";
    const range = lineRange(value);
    if (range) Object.assign(ref, range);
    return ref;
  }
  function lineRange(value = {}) {
    const start = Number(value.start_line), end = Number(value.end_line ?? value.start_line);
    return Number.isSafeInteger(start) && start > 0 && Number.isSafeInteger(end) && end >= start
      ? { start_line: start, end_line: end } : null;
  }
  function referenceFromHref(href) {
    const path = pathFromHref(href);
    if (!path) return null;
    const match = String(href).match(/#L(\d+)(?:-L?(\d+))?$/i);
    return reference({ path, ...(match ? { start_line: match[1], end_line: match[2] || match[1] } : {}) });
  }
  function follow(event, { onFile, onExternal, onError }) {
    const anchor = event.target.closest("a[href]");
    if (!anchor || anchor.hasAttribute?.("download")) return;
    const href = anchor.getAttribute("href") || "";
    if (!href || href.startsWith("#") || /^blob:/i.test(href)) return;
    event.preventDefault(); event.stopPropagation();
    try {
      if (/^https?:\/\//i.test(href)) {
        if (!onExternal) throw new Error("当前运行环境无法打开外部链接");
        return Promise.resolve(onExternal(href)).catch(onError);
      }
      const path = pathFromHref(href);
      if (!path) throw new Error("不支持此文件链接");
      return Promise.resolve(onFile(path, anchor, referenceFromHref(href))).catch(onError);
    } catch (error) { onError(new Error(`文件链接无效: ${error.message}`)); }
  }
  function createPreview({ runtime, isCurrent, onClose, onCurrent, onReference, getPreview = () => root.FocusFilePreview }) {
    let fileLinkPreview = null;
    const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
    function releaseFileLinkPreview() {
      if (fileLinkPreview?.url) URL.revokeObjectURL(fileLinkPreview.url);
      fileLinkPreview = null;
    }
    async function mount(container, target) {
      // Preserve main's shared previews and native exits, passing the complete link
      // identity through the same server-side workspace/slot validation.
      if (target.view === "current" && getPreview()) {
        releaseFileLinkPreview();
        container.innerHTML = `<header class="file-panel-head"><strong>${escapeHtml(target.path)}</strong><small>当前磁盘文件</small><button type="button" data-file-link-close>关闭</button></header><p class="muted">${escapeHtml(target.workspace_id)}${target.slot_id ? ` · 工作树 ${escapeHtml(target.slot_id)}` : ""}</p><div class="file-link-content" data-linked-preview></div>`;
        container.querySelector("[data-file-link-close]")?.addEventListener("click", onClose);
        return getPreview().mount(container.querySelector("[data-linked-preview]"), {
          name: target.path, target: { task_id: target.task_id, link: reference(target) },
          onReference: onReference ? value => onReference(value, target.task_id) : undefined,
        });
      }
      if (fileLinkPreview?.target !== target) {
        releaseFileLinkPreview();
        const entry = { target, url: null, text: null, media: null, error: null };
        fileLinkPreview = entry;
        entry.pending = (async () => {
          try {
            const response = await fetch(`${runtime.apiBase}/desktop/api/tasks/${encodeURIComponent(target.task_id)}/file-links/content`, {
              method: "POST",
              headers: { "X-Focus-Session": runtime.session, "Content-Type": "application/json" },
              body: JSON.stringify(reference(target)),
            });
            if (!response.ok) {
              const body = await response.text();
              let detail; try { detail = JSON.parse(body).detail; } catch {}
              throw new Error(typeof detail === "string" ? detail : body || `HTTP ${response.status}`);
            }
            entry.identity = JSON.parse(decodeURIComponent(response.headers.get("X-Focus-File-Reference")));
            entry.media = (response.headers.get("Content-Type") || "").split(";")[0];
            if (entry.media === "text/plain") entry.text = await response.text();
            else {
              const blob = await response.blob();
              if (fileLinkPreview === entry) entry.url = URL.createObjectURL(blob);
            }
          } catch (error) { entry.error = error.message; }
        })();
      }
      const entry = fileLinkPreview;
      const paint = () => {
        if (fileLinkPreview !== entry || !isCurrent(target)) return;
        const historical = target.view === "history";
        const version = entry.identity?.sha256 || target.version_id || target.result_key || "";
        container.innerHTML = `<header class="file-panel-head"><span class="file-panel-title" title="${escapeHtml(target.path)}"><strong>${escapeHtml(target.path.split(/[\\/]/).pop())}</strong><small>${historical ? "历史版本" : "当前磁盘文件"}${version ? ` · ${escapeHtml(version.slice(0, 12))}` : ""}</small></span><span class="file-panel-tools">${historical ? '<button type="button" data-file-link-current>打开当前磁盘文件</button>' : ""}<button type="button" data-file-link-close>关闭</button></span></header><p class="muted">${escapeHtml(entry.identity?.path || target.path)} · ${escapeHtml(target.workspace_id)}${entry.identity?.slot_id || target.slot_id ? ` · 工作树 ${escapeHtml(entry.identity?.slot_id || target.slot_id)}` : ""}${historical ? " · 固定版本，不随磁盘更新" : " · 此处显示的是读取时的磁盘内容"}</p><div class="file-link-content">${entry.error ? `<p role="alert">${escapeHtml(entry.error)}</p>` : entry.text != null ? `<pre>${escapeHtml(entry.text)}</pre>` : entry.url ? entry.media === "application/pdf" ? `<iframe title="PDF 文件预览" src="${escapeHtml(entry.url)}"></iframe>` : `<img alt="文件预览" src="${escapeHtml(entry.url)}">` : "正在读取文件…"}</div>`;
        container.querySelector("[data-file-link-close]")?.addEventListener("click", onClose);
        container.querySelector("[data-file-link-current]")?.addEventListener("click", () => onCurrent({
          path: target.path, workspace_id: target.workspace_id, slot_id: target.slot_id, run_id: target.run_id, view: "current",
          ...lineRange(target),
        }, target.task_id));
        if (entry.text != null && getPreview()?.renderText) {
          getPreview().renderText(container.querySelector(".file-link-content"), entry.text, target.path, {
            range: lineRange(target),
            onReference: onReference ? value => onReference({ ...reference(target), ...value }, target.task_id) : undefined,
          });
        }
      };
      paint();
      await entry.pending;
      paint();
    }
    return { mount, release: releaseFileLinkPreview };
  }
  return Object.freeze({ pathFromHref, referenceFromHref, lineRange, reference, follow, createPreview });
});
