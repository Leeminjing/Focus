/* Material viewer adapter. Shared previews handle images, PDF pages and text;
   DOCX files can open an isolated ONLYOFFICE editor in the same workbench panel. */
(function (root) {
  "use strict";

  let openedMaterial = null;
  let openedAppState = null;

  function escapeHtml(value = "") {
    return String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  }

  function supportsMaterial(materialOrName) {
    const name = typeof materialOrName === "string"
      ? materialOrName : materialOrName?.relative_path || materialOrName?.path || "";
    return !!name;
  }

  function mountPanel(container, material, appState, { onReference, onClose } = {}) {
    const name = material?.relative_path || material?.path || "";
    const target = { task_id: appState.activeTaskId, content_ref: name };
    const close = onClose || (() => root.__focusCloseFilePanel?.());
    const editor = root.FocusDocxEditor;
    if (/\.docx$/i.test(name) && editor?.mount) {
      editor.mount(container, { ...material, relative_path: name }, appState, { onClose: close });
      return;
    }
    editor?.destroy();
    container.innerHTML = `<header class="file-panel-head"><span class="file-panel-title"><span class="workspace-kicker">FILE WORKBENCH</span><strong title="${escapeHtml(name)}">${escapeHtml(name)}</strong></span><button type="button" class="text-button" data-viewer-close>关闭</button></header><div class="file-panel-body file-viewer-content" data-generic-file></div>`;
    container.querySelector("[data-viewer-close]").addEventListener("click", close);
    const host = container.querySelector("[data-generic-file]");
    if (root.FocusFilePreview?.mount) {
      void root.FocusFilePreview.mount(host, { name, target, onReference });
    } else {
      host.textContent = "文件预览资源不可用，请使用系统应用打开文件。";
    }
  }

  function openMaterial(material, appState) {
    if (!supportsMaterial(material)) return false;
    openedMaterial = material;
    openedAppState = appState;
    appState.view = "spatial-viewer";
    return true;
  }

  function render(app, appState = openedAppState) {
    if (!openedMaterial || !appState) return;
    mountPanel(app, openedMaterial, appState, { onClose: () => root.__focusBackToFocus?.() });
  }

  // The host discovers this adapter through the existing plugin view registration.
  root.FocusSpatialViewer = { render, openMaterial, mountPanel, supportsMaterial };
})(typeof globalThis === "object" ? globalThis : this);
