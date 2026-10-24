/* Selection presents the actual native window; it never reloads, hides another
 * window or reacquires control. Live snapshots use the existing session auth. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusContextBrowser = api;
})(globalThis, function () {
  "use strict";
  const labels = { unbound: "运行时创建独立浏览器", available: "可申请控制", automatic: "Agent 控制中",
    taking_over: "接管中，已发动作仍可能收尾", manual: "人工控制，请在浏览器点击交还",
    paused: "已暂停，恢复窗口后仍需点击交还", faulted: "浏览器故障，请显式更换绑定",
    closed: "浏览器已关闭，请显式更换绑定", disconnected: "连接已断开，原窗口可供人工使用" };
  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  function render(id) {
    return `<span class="context-browser-controls" data-context-browser="${escape(id)}"><button type="button" data-action="context-browser-present" data-context-id="${escape(id)}">浏览器</button><span role="status" data-browser-status="${escape(id)}"></span><button type="button" data-action="context-browser-binding" data-context-id="${escape(id)}">浏览器绑定</button></span>`;
  }
  function create({ runtime, api, getContexts = () => [], onStatus = () => {}, EventSourceImpl = globalThis.EventSource }) {
    let selected = null;
    let source = null;
    const clientId = globalThis.crypto.randomUUID();
    let sequence = 0;
    const snapshots = new Map();
    const path = id => `/desktop/api/contexts/${encodeURIComponent(id)}/browser`;
    function patch() {
      for (const node of document.querySelectorAll("[data-browser-status]")) {
        const value = snapshots.get(node.dataset.browserStatus);
        if (value) node.textContent = `${labels[value.state] || value.state}${value.shared_context_ids?.length > 1 ? ` · ${value.shared_context_ids.length} 个 Context 共享` : ""}`;
      }
    }
    function update(snapshot) { snapshots.set(snapshot.context_id, snapshot); patch(); }
    async function select(contextId, { present = true } = {}) {
      if (!contextId) return;
      const presentation = { client_id: clientId, sequence: ++sequence };
      if (selected !== contextId) {
        selected = contextId;
        source?.close();
        const url = `${String(runtime.apiBase || "").replace(/\/$/, "")}${path(contextId)}/stream?session=${encodeURIComponent(runtime.session)}`;
        source = new EventSourceImpl(url);
        source.addEventListener("browser", event => { if (selected === contextId) update(JSON.parse(event.data)); });
      }
      try {
        const result = await api(`${path(contextId)}${present ? "/present" : ""}`, present ? { method: "POST", body: JSON.stringify(presentation) } : {});
        update(result);
        if (selected === contextId && result.foreground === false) onStatus("浏览器已展示；Windows 未允许前台激活，可从任务栏查看。", false);
      } catch (error) { if (selected === contextId) onStatus(error.message, true); }
    }
    function binding(contextId, contexts) {
      const dialog = document.createElement("dialog");
      dialog.className = "patrol-dialog glass-surface context-browser-dialog";
      dialog.innerHTML = `<form><h2>浏览器绑定</h2><p>结束当前 Context 的 Run 后可更换。共享会操作同一个真实页面；Run 结束保留页面。更换绑定后，无其他 Context 使用的原窗口将关闭。</p><label>绑定方式<select name="source"><option value="">创建新的独立浏览器</option>${contexts.filter(item => item.task_id !== contextId).map(item => `<option value="${escape(item.task_id)}">共享：${escape(item.title)}</option>`).join("")}</select></label><p role="alert"></p><button type="submit">应用绑定</button><button type="button" data-dismiss>取消</button></form>`;
      dialog.querySelector("[data-dismiss]").onclick = () => dialog.close();
      dialog.addEventListener("close", () => dialog.remove(), { once: true });
      dialog.querySelector("form").onsubmit = async event => {
        event.preventDefault();
        const button = dialog.querySelector('[type="submit"]');
        button.disabled = true;
        try {
          update(await api(path(contextId), { method: "PUT", body: JSON.stringify({ source_context_id: dialog.querySelector("select").value || null }) }));
          dialog.close();
          await select(contextId);
        } catch (error) { dialog.querySelector('[role="alert"]').textContent = error.message; }
        finally { button.disabled = false; }
      };
      document.body.append(dialog);
      dialog.showModal();
    }
    document.addEventListener("click", event => {
      const button = event.target.closest('[data-action^="context-browser-"]');
      if (!button) return;
      event.preventDefault(); event.stopPropagation();
      const id = button.dataset.contextId;
      if (button.dataset.action === "context-browser-present") void select(id);
      else if (button.dataset.action === "context-browser-binding") binding(id, getContexts(id));
    }, true);
    return { select, patch, binding, render,
      watch(id) { if (selected !== id) void select(id, { present: false }); else patch(); },
      close() { source?.close(); source = null; selected = null; } };
  }
  return { create, render, labels };
});
