/* DOCX editor adapter: opens isolated ONLYOFFICE sessions and saves document changes. */
(function (root) {
  "use strict";

  const API_PREFIX = "/desktop/api/plugin/spatial-patrol/docx";
  const SAVE_POLL_MS = 250;
  const SAVE_TIMEOUT_MS = 30000;
  let scriptPromise = null;
  let active = null;

  function escapeHtml(value = "") {
    return String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  }

  async function responseError(response) {
    const text = await response.text();
    try { return JSON.parse(text).detail || text; } catch { return text || `HTTP ${response.status}`; }
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("X-Focus-Session", root.focusDesktop?.runtime?.().session || "focus-dev-session");
    if (options.body) headers.set("Content-Type", "application/json");
    const response = await fetch(`${API_PREFIX}${path}`, { ...options, headers });
    if (!response.ok) throw new Error(await responseError(response));
    return response.json();
  }

  function loadDocsApi(origin) {
    if (root.DocsAPI?.DocEditor) return Promise.resolve();
    if (scriptPromise) return scriptPromise;
    scriptPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = `${origin}/web-apps/apps/api/documents/api.js`;
      script.async = true;
      const timer = setTimeout(() => reject(new Error("Document Server API 加载超时")), 20000);
      script.onload = () => { clearTimeout(timer); resolve(); };
      script.onerror = () => { clearTimeout(timer); scriptPromise = null; reject(new Error("Document Server API 加载失败")); };
      document.head.append(script);
    });
    return scriptPromise;
  }

  function isCurrent(instance) {
    return !!instance && active === instance;
  }

  function statusFor(instance, message, tone = "neutral") {
    if (!isCurrent(instance)) return;
    const node = instance.container?.querySelector("[data-docx-status]");
    if (!node) return;
    node.textContent = message;
    node.dataset.tone = tone;
  }

  function shell(name) {
    return `
      <section class="focus-docx-editor">
        <header class="focus-docx-toolbar">
          <span class="focus-docx-title"><span class="workspace-kicker">FILE WORKBENCH · DOCX</span><strong title="${escapeHtml(name)}">${escapeHtml(name)}</strong></span>
          <span class="focus-docx-status" data-docx-status>选择打开方式</span>
          <button type="button" class="text-button" data-docx-save hidden>保存</button>
          <button type="button" class="text-button" data-docx-close>关闭</button>
        </header>
        <div class="focus-docx-stage" data-docx-stage>
          <div class="focus-docx-mode" data-docx-mode>
            <section class="focus-docx-mode-card">
              <h3>用 Word 式编辑器打开</h3>
              <p>保留分页、样式、表格、图片、页眉页脚、水印与页面设置。</p>
              <div class="focus-docx-mode-actions">
                <button type="button" data-docx-open="view">只读打开</button>
                <button type="button" class="primary" data-docx-open="edit">编辑文档</button>
              </div>
            </section>
          </div>
          <div class="focus-docx-host" data-docx-host hidden></div>
        </div>
        <footer class="focus-docx-footer">文档修改通过编辑器保存到工作区文件。</footer>
      </section>`;
  }

  function renderErrorFor(instance, error) {
    if (!isCurrent(instance)) return;
    const stage = instance.container?.querySelector("[data-docx-stage]");
    if (stage) stage.innerHTML = `<div class="focus-docx-error"><strong>DOCX 编辑插件不可用</strong><p>${escapeHtml(error.message || error)}</p></div>`;
    statusFor(instance, "插件不可用", "danger");
  }

  async function openEditor(mode, instance = active) {
    if (!isCurrent(instance) || instance.opening) return;
    instance.opening = true;
    instance.mode = mode;
    statusFor(instance, "正在准备开源编辑器…", "warning");
    try {
      const payload = await api("/sessions", {
        method: "POST",
        body: JSON.stringify({
          task_id: instance.task.task_id,
          content_ref: instance.material.relative_path,
          mode,
        }),
      });
      if (!isCurrent(instance)) return;
      instance.sessionId = payload.session.session_id;
      instance.mode = mode;
      await loadDocsApi(payload.document_server_url);
      if (!isCurrent(instance)) return;
      const host = instance.container.querySelector("[data-docx-host]");
      const modeNode = instance.container.querySelector("[data-docx-mode]");
      host.hidden = false;
      modeNode.hidden = true;
      host.id = `focus-docx-${instance.sessionId}`;
      const config = payload.config;
      config.width = "100%";
      config.height = "100%";
      config.events = {
        onDocumentReady: () => statusFor(instance, mode === "edit" ? "可编辑" : "只读", "success"),
        onDocumentStateChange: event => {
          if (!isCurrent(instance)) return;
          instance.dirty = event?.data === true;
          if (instance.dirty) instance.saved = false;
          if (instance.saving) return;
          if (instance.dirty) statusFor(instance, "编辑器内有未保存更改", "warning");
          else if (!instance.saved) statusFor(instance, "已同步", "success");
        },
        onError: event => handleOpenError(instance, new Error(event?.data?.errorDescription || `编辑器错误 ${event?.data?.errorCode || ""}`)),
        onRequestClose: () => closeEditor(instance),
      };
      instance.editor = new root.DocsAPI.DocEditor(host.id, config);
      instance.container.querySelector("[data-docx-save]").hidden = mode !== "edit";
      instance.pollTimer = setInterval(() => pollSession(instance), 2000);
    } catch (error) {
      await handleOpenError(instance, error);
    } finally {
      instance.opening = false;
    }
  }

  async function handleOpenError(instance, error) {
    if (!isCurrent(instance)) return;
    if (instance.mode !== "view" || !root.FocusFilePreview) { renderErrorFor(instance, error); return; }
    clearInterval(instance.pollTimer);
    try { instance.editor?.destroyEditor?.(); } catch {}
    instance.editor = null;
    const stage = instance.container.querySelector("[data-docx-stage]");
    stage.innerHTML = '<div data-generic-file></div>';
    statusFor(instance, "编辑服务不可用，已回退只读文本", "warning");
    await root.FocusFilePreview.mount(stage.querySelector("[data-generic-file]"), {
      name: instance.material.relative_path,
      target: { task_id: instance.task.task_id, content_ref: instance.material.relative_path },
    });
  }

  async function pollSession(instance = active) {
    if (!isCurrent(instance) || !instance.sessionId) return;
    try {
      const session = await api(`/sessions/${encodeURIComponent(instance.sessionId)}`);
      if (!isCurrent(instance)) return;
      instance.dirty = session.dirty;
      if (instance.dirty) instance.saved = false;
      if (instance.saving) return session;
      const presentation = sessionPresentation(session);
      if (presentation) {
        instance.saved = presentation.message === "已保存到磁盘";
        statusFor(instance, presentation.message, presentation.tone);
      }
      return session;
    } catch (error) {
      statusFor(instance, `会话状态不可用：${error.message}`, "danger");
    }
  }

  function sessionPresentation(session) {
    if (session?.last_error) return { message: session.last_error, tone: "danger" };
    if (session?.status === "saved") return { message: "已保存到磁盘", tone: "success" };
    if (session?.status === "recoverable") return { message: "存在可恢复版本", tone: "warning" };
    if (session?.dirty) return { message: "编辑器内有未保存更改", tone: "warning" };
    if (session?.callback_status === 6) return { message: "已保存到磁盘", tone: "success" };
    return null;
  }

  function saveEvidenceAdvanced(session, baseline) {
    if (!session || session.dirty || session.last_error) return false;
    return session.saved_hash !== baseline.saved_hash
      || Number(session.document_version) > Number(baseline.document_version);
  }

  async function waitForSavedSession(instance, baseline) {
    const deadline = Date.now() + SAVE_TIMEOUT_MS;
    while (isCurrent(instance)) {
      const session = await api(`/sessions/${encodeURIComponent(instance.sessionId)}`);
      if (session.last_error) throw new Error(session.last_error);
      if (saveEvidenceAdvanced(session, baseline)) return session;
      if (Date.now() >= deadline) throw new Error("等待 Document Server 保存回调超时");
      await new Promise(resolve => setTimeout(resolve, SAVE_POLL_MS));
    }
    throw new Error("DOCX 编辑器已关闭");
  }

  async function forceSave(instance = active) {
    if (!isCurrent(instance) || !instance.sessionId) return;
    instance.saving = true;
    statusFor(instance, "正在保存…", "warning");
    try {
      const baseline = await api(`/sessions/${encodeURIComponent(instance.sessionId)}`);
      await api(`/sessions/${encodeURIComponent(instance.sessionId)}/force-save`, { method: "POST" });
      const session = await waitForSavedSession(instance, baseline);
      if (!isCurrent(instance)) return;
      instance.dirty = session.dirty;
      instance.saved = true;
      statusFor(instance, "已保存到磁盘", "success");
    } catch (error) {
      statusFor(instance, `保存未完成：${error.message}`, "danger");
    } finally {
      if (isCurrent(instance)) instance.saving = false;
    }
  }

  async function closeEditor(instance = active) {
    if (!isCurrent(instance)) return;
    let discard = false;
    if (instance.dirty) {
      const confirmed = root.confirm("文档仍有未保存更改。放弃这些更改并关闭吗？");
      if (!confirmed) return;
      discard = true;
    }
    const sessionId = instance.sessionId;
    const onClose = instance.onClose;
    if (sessionId) {
      try {
        await api(`/sessions/${encodeURIComponent(sessionId)}/close`, {
          method: "POST", body: JSON.stringify({ discard_unsaved: discard }),
        });
      } catch (error) {
        statusFor(instance, `关闭失败：${error.message}`, "danger");
        return;
      }
    }
    if (!isCurrent(instance)) return;
    destroy(instance);
    onClose?.();
  }

  function destroy(instance = active) {
    if (!isCurrent(instance)) return;
    clearInterval(instance.pollTimer);
    try { instance.editor?.destroyEditor?.(); } catch (error) { console.warn("DOCX editor destroy failed", error); }
    instance.container.innerHTML = "";
    active = null;
  }

  function mount(container, material, appState, options = {}) {
    destroy();
    const task = appState.tasks.find(item => item.task_id === appState.activeTaskId);
    container.innerHTML = shell(material.relative_path);
    const instance = {
      container, material, task, mode: null, sessionId: null, editor: null,
      dirty: false, opening: false, saving: false, saved: false, pollTimer: null,
      onClose: options.onClose,
    };
    active = instance;
    if (root.FocusFilePreview) {
      const target = { task_id: task.task_id, content_ref: material.relative_path };
      const exits = document.createElement("div");
      exits.innerHTML = root.FocusFilePreview.toolbar(target, material.relative_path);
      container.querySelector(".focus-docx-toolbar").after(exits);
      root.FocusFilePreview.bindActions(exits, target, material.relative_path);
    }
    container.querySelectorAll("[data-docx-open]").forEach(button => button.addEventListener("click", () => openEditor(button.dataset.docxOpen, instance)));
    container.querySelector("[data-docx-save]").addEventListener("click", () => forceSave(instance));
    container.querySelector("[data-docx-close]").addEventListener("click", () => closeEditor(instance));
    return true;
  }

  root.FocusDocxEditor = {
    mount,
    destroy,
    forceSave,
    close: closeEditor,
    hasUnsavedChanges: () => !!active?.dirty,
    _test: { loadDocsApi, responseError, saveEvidenceAdvanced, sessionPresentation },
  };
})(typeof globalThis === "object" ? globalThis : this);
