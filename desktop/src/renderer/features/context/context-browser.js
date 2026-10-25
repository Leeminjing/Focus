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
  const interactionLabels = { in_progress: "下载中", completed: "已完成", cancelled: "已取消", interrupted: "已中断",
    pending: "等待用户决定", allowed_once: "已允许本次请求", denied: "已拒绝", failed: "决定失败" };
  const permissionLabels = { geolocation: "位置", camera: "摄像头", microphone: "麦克风", notifications: "通知",
    clipboard_read: "读取剪贴板", sensors: "传感器", automatic_downloads: "自动下载多个文件", file_read_write: "文件读写",
    autoplay: "自动播放", local_fonts: "本机字体", midi: "MIDI 设备", window_management: "屏幕与窗口" };
  const reasonLabels = { human_cancel: "用户取消", human_decision: "用户决定", no_progress_timeout: "长时间没有下载进展",
    download_limit: "同时下载数量已达上限", same_page_url_already_downloading: "该页面的同一地址已在下载",
    disconnected: "浏览器连接断开", browser_faulted: "浏览器故障", timeout: "等待超时", page_navigated: "来源页面已导航",
    prompt_closed: "用户关闭权限提示", host_setup_failed: "无法创建下载", cancel_failed: "无法确认取消结果" };
  function statusLabel(value) {
    if (value.state === 'disconnected' && value.detail?.reason === 'legacy_session') return '旧版会话不可恢复，请显式更换绑定';
    if (value.state === 'disconnected' && value.detail?.message) return value.detail.message;
    const restored = value.recovered && !['closed', 'disconnected', 'faulted'].includes(value.state);
    const suffix = restored && value.interrupted_operation?.result_unknown ? ' · 上次操作结果未知，请先检查页面' : '';
    const prefix = restored ? '原会话已恢复 · ' : '';
    const sites = value.site_interactions || {};
    if (["manual", "taking_over", "paused"].includes(value.state)) {
      if (sites.permissions?.some(item => item.state === "pending")) return prefix + "网站权限待决定，请在原生浏览器处理" + suffix;
      if (sites.downloads?.some(item => item.state === "in_progress")) return prefix + "下载进行中，Agent 已暂停" + suffix;
    }
    return prefix + (labels[value.state] || value.state) + suffix;
  }
  function recoverySummary(value = {}) {
    if (!value.recovered) return '';
    const operation = value.interrupted_operation;
    if (!operation?.kind) return '已连接原宿主并保留原页面。旧 Run 和控制令牌不会恢复；交还后需要新观察。';
    const kinds = { navigate: '导航', observe: '观察', act: '输入操作' };
    return `上次中断：${kinds[operation.kind] || operation.kind}；来源页面：${operation.page_id}；已派发 ${operation.dispatched_steps} 步，已完成 ${operation.completed_steps} 步；${operation.settled ? '回调已收尾' : '仍在收尾'}。${operation.result_unknown ? '网页业务结果未知，请先检查页面，禁止自动补发。' : '未报告已派发副作用。'}`;
  }
  function renderInteractions(value = {}) {
    const downloads = (value.downloads || []).map(item => {
      const received = Math.max(0, Number(item.bytes_received) || 0), total = Number(item.total_bytes);
      return `<article class="context-browser-interaction"><strong>${escape(interactionLabels[item.state] || item.state)}</strong><p>${received} / ${total > 0 ? total : "大小未知"} 字节</p>${total > 0 ? `<progress aria-label="下载进度" max="${total}" value="${Math.min(received, total)}"></progress>` : ""}<p>${escape(item.url)}</p><code>${escape(item.path || "尚未确定保存路径")}</code><small>来源页面：${escape(item.page_id)}</small>${item.reason ? `<p>原因：${escape(reasonLabels[item.reason] || item.reason)}</p>` : ""}</article>`;
    }).join("");
    const permissions = (value.permissions || []).map(item => `<article class="context-browser-interaction"><strong>${escape(permissionLabels[item.kind] || item.kind)} · ${escape(interactionLabels[item.state] || item.state)}</strong><p>${escape(item.url)}</p><small>来源页面：${escape(item.page_id)}</small>${item.reason ? `<p>原因：${escape(reasonLabels[item.reason] || item.reason)}</p>` : ""}</article>`).join("");
    const rejected = (value.rejected || []).map(item => `<p>下载未接受：${escape(reasonLabels[item.reason] || item.reason)}${item.page_id ? ` · 来源页面：${escape(item.page_id)}` : ""}</p>`).join("");
    return `<p class="context-browser-interaction-guidance">取消下载请在原生浏览器按 Ctrl+J；网站权限请在原生提示中决定。处理结束后仍需交还，Agent 会重新观察。</p><section><h3>下载</h3>${downloads || "<p>尚无下载记录。</p>"}${rejected}</section><section><h3>网站权限</h3>${permissions || "<p>尚无网站权限请求。</p>"}</section>`;
  }
  function render(id) {
    return `<span class="context-browser-controls" data-context-browser="${escape(id)}"><button type="button" data-action="context-browser-present" data-context-id="${escape(id)}">浏览器</button><button type="button" data-action="context-browser-pages" data-context-id="${escape(id)}">页面</button><button type="button" data-action="context-browser-downloads" data-context-id="${escape(id)}">下载与权限</button><span role="status" data-browser-status="${escape(id)}"></span><button type="button" data-action="context-browser-binding" data-context-id="${escape(id)}">浏览器绑定</button></span>`;
  }
  function create({ runtime, api, getContexts = () => [], onStatus = () => {}, EventSourceImpl = globalThis.EventSource }) {
    let selected = null;
    let source = null;
    let pageDialog = null;
    let interactionDialog = null;
    const clientId = globalThis.crypto.randomUUID();
    let sequence = 0;
    const snapshots = new Map();
    const path = id => `/desktop/api/contexts/${encodeURIComponent(id)}/browser`;
    function patch() {
      for (const node of document.querySelectorAll("[data-browser-status]")) {
        const value = snapshots.get(node.dataset.browserStatus);
        if (value) node.textContent = `${statusLabel(value)}${value.shared_context_ids?.length > 1 ? ` · ${value.shared_context_ids.length} 个 Context 共享` : ""}`;
      }
      for (const node of document.querySelectorAll('[data-browser-recovery]')) {
        node.textContent = recoverySummary(snapshots.get(node.dataset.browserRecovery));
      }
    }
    function update(snapshot) {
      const previous = snapshots.get(snapshot.context_id);
      snapshots.set(snapshot.context_id, snapshot); patch();
      if (pageDialog?.contextId === snapshot.context_id &&
          (previous?.pages_revision !== snapshot.pages_revision || previous?.state !== snapshot.state || previous?.browser_id !== snapshot.browser_id)) void pageDialog.refresh();
      if (interactionDialog?.contextId === snapshot.context_id &&
          (previous?.interactions_revision !== snapshot.interactions_revision || previous?.state !== snapshot.state || previous?.browser_id !== snapshot.browser_id)) void interactionDialog.refresh();
    }
    async function select(contextId, { present = true, pageId = null } = {}) {
      if (!contextId) return;
      const presentation = { client_id: clientId, sequence: ++sequence };
      if (pageId) presentation.page_id = pageId;
      if (selected !== contextId) {
        pageDialog?.dialog.close();
        interactionDialog?.dialog.close();
        selected = contextId;
        source?.close();
        const url = `${String(runtime.apiBase || "").replace(/\/$/, "")}${path(contextId)}/stream?session=${encodeURIComponent(runtime.session)}`;
        const connection = source = new EventSourceImpl(url);
        source.addEventListener("browser", event => { if (source === connection && selected === contextId) update(JSON.parse(event.data)); });
      }
      try {
        const result = await api(`${path(contextId)}${present ? "/present" : ""}`, present ? { method: "POST", body: JSON.stringify(presentation) } : {});
        if (selected !== contextId || presentation.sequence !== sequence) return;
        update(result);
        if (selected === contextId && result.foreground === false) onStatus("浏览器已展示；Windows 未允许前台激活，可从任务栏查看。", false);
      } catch (error) { if (selected === contextId && presentation.sequence === sequence) onStatus(error.message, true); }
    }
    function pages(contextId) {
      interactionDialog?.dialog.close();
      pageDialog?.dialog.close();
      const dialog = document.createElement("dialog");
      dialog.className = "patrol-dialog glass-surface context-browser-dialog context-browser-pages";
      dialog.innerHTML = `<form method="dialog"><h2>浏览器页面</h2><p>这些页面共享登录状态与控制权。展示另一页后，Agent 必须重新观察。</p><p data-browser-recovery="${escape(contextId)}"></p><p role="alert"></p><div class="context-browser-page-list" aria-live="polite"></div><div><button type="button" data-refresh>刷新列表</button><button type="submit">关闭</button></div></form>`;
      const list = dialog.querySelector(".context-browser-page-list");
      const alert = dialog.querySelector('[role="alert"]');
      let loading = false, dirty = false;
      const panel = { contextId, dialog, refresh };
      async function refresh() {
        dirty = true;
        if (loading) return;
        loading = true;
        try {
          while (dirty && pageDialog === panel) {
            dirty = false;
            const value = await api(`${path(contextId)}/pages`);
            if (pageDialog !== panel || dirty) continue;
            alert.textContent = "";
            list.innerHTML = value.pages.length ? value.pages.map(page => `<article class="context-browser-page"><div><strong>${escape(page.title || "无标题页面")}</strong>${page.page_id === value.selected_page_id ? '<span class="context-browser-current">当前页</span>' : ""}<p>${escape(page.url || "about:blank")}</p></div><button type="button" data-page-id="${escape(page.page_id)}" ${page.ready ? "" : "disabled"}>展示</button></article>`).join("") : "<p>尚无页面；运行任务或创建浏览器绑定后可查看。</p>";
          }
        } catch (error) { if (pageDialog === panel) alert.textContent = error.message; }
        finally { loading = false; }
      }
      pageDialog = panel;
      dialog.querySelector("[data-refresh]").onclick = () => void refresh();
      dialog.addEventListener("click", async event => {
        const button = event.target.closest("[data-page-id]");
        if (!button) return;
        button.disabled = true;
        try { await select(contextId, { pageId: button.dataset.pageId }); await refresh(); }
        finally { button.disabled = false; }
      });
      dialog.addEventListener("close", () => { if (pageDialog === panel) pageDialog = null; dialog.remove(); }, { once: true });
      document.body.append(dialog);
      dialog.showModal();
      patch();
      void refresh();
    }
    function downloads(contextId) {
      pageDialog?.dialog.close();
      interactionDialog?.dialog.close();
      const dialog = document.createElement("dialog");
      dialog.className = "patrol-dialog glass-surface context-browser-dialog context-browser-downloads";
      dialog.innerHTML = `<form method="dialog"><h2>下载与网站权限</h2><p role="alert"></p><div class="context-browser-interaction-list"></div><div><button type="button" data-refresh>刷新状态</button><button type="button" data-present>展示原生浏览器</button><button type="submit">关闭</button></div></form>`;
      const list = dialog.querySelector(".context-browser-interaction-list"), alert = dialog.querySelector('[role="alert"]');
      let loading = false, dirty = false;
      const panel = { contextId, dialog, refresh };
      async function refresh() {
        dirty = true;
        if (loading) return;
        loading = true;
        try {
          while (dirty && interactionDialog === panel) {
            dirty = false;
            const value = await api(`${path(contextId)}/downloads`);
            if (interactionDialog !== panel || dirty) continue;
            alert.textContent = ""; list.innerHTML = renderInteractions(value);
          }
        } catch (error) { if (interactionDialog === panel) alert.textContent = error.message; }
        finally { loading = false; }
      }
      interactionDialog = panel;
      dialog.querySelector("[data-refresh]").onclick = () => void refresh();
      dialog.querySelector("[data-present]").onclick = () => void select(contextId);
      dialog.addEventListener("close", () => { if (interactionDialog === panel) interactionDialog = null; dialog.remove(); }, { once: true });
      document.body.append(dialog); dialog.showModal(); void refresh();
    }
    function binding(contextId, contexts) {
      const dialog = document.createElement("dialog");
      dialog.className = "patrol-dialog glass-surface context-browser-dialog";
      dialog.innerHTML = `<form><h2>浏览器绑定</h2><p>结束当前 Context 的 Run 后可更换。共享会使用同一浏览器会话及其全部页面；Run 结束保留页面。更换绑定后，无其他 Context 使用的原窗口将关闭。</p><label>绑定方式<select name="source"><option value="">创建新的独立浏览器</option>${contexts.filter(item => item.task_id !== contextId).map(item => `<option value="${escape(item.task_id)}">共享：${escape(item.title)}</option>`).join("")}</select></label><p role="alert"></p><button type="submit">应用绑定</button><button type="button" data-dismiss>取消</button></form>`;
      const details = document.createElement('p');
      details.dataset.browserRecovery = contextId;
      dialog.querySelector('form').append(details);
      if (snapshots.get(contextId)?.can_reconnect && snapshots.get(contextId)?.state === 'disconnected') {
        const reconnect = document.createElement('button');
        reconnect.type = 'button'; reconnect.textContent = '重新连接原宿主';
        reconnect.onclick = async () => {
          reconnect.disabled = true;
          try {
            const result = await api(`${path(contextId)}/recover`, { method: 'POST' });
            update(result);
            if (result.state === 'disconnected') dialog.querySelector('[role="alert"]').textContent = result.detail?.message || '原宿主暂不可连接';
            else { dialog.close(); onStatus('已重新连接原会话，请在原生浏览器检查页面并交还后继续'); }
          } catch (error) { dialog.querySelector('[role="alert"]').textContent = error.message; }
          finally { reconnect.disabled = false; }
        };
        dialog.querySelector('form').append(reconnect);
      }
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
      patch();
    }
    document.addEventListener("click", event => {
      const button = event.target.closest('[data-action^="context-browser-"]');
      if (!button) return;
      event.preventDefault(); event.stopPropagation();
      const id = button.dataset.contextId;
      if (button.dataset.action === "context-browser-present") void select(id);
      else if (button.dataset.action === "context-browser-pages") pages(id);
      else if (button.dataset.action === "context-browser-downloads") downloads(id);
      else if (button.dataset.action === "context-browser-binding") binding(id, getContexts(id));
    }, true);
    return { select, patch, binding, pages, downloads, render,
      watch(id) { if (selected !== id) void select(id, { present: false }); else patch(); },
      close() { pageDialog?.dialog.close(); interactionDialog?.dialog.close(); source?.close(); source = null; selected = null; } };
  }
  return { create, render, labels, statusLabel, renderInteractions, recoverySummary };
});
