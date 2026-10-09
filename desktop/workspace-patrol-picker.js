/* 本文件对外提供 FocusWorkspacePatrolPicker.create 的工作区选择页面。
 * 输入为原 Loop API、文件夹绑定回调、进入回调及上次工作区身份；输出为搜索列表、选中详情和显式进入操作。
 * 工作流为读取已有工作区，按名称和完整路径筛选，以身份区分同名项目；选择只更新详情，进入复用原绑定，
 * 离开后忽略迟到响应，不创建 Context、Run 或另一份持久状态。图标与转义复用宿主资产和 Patrol view。
 * 示例：const picker = FocusWorkspacePatrolPicker.create({ api, chooseFolder, onEnter }); await picker.mount(host)。
 */
(function (root, factory) {
  const view = root.FocusWorkspacePatrolView || (typeof require === "function" ? require("./workspace-patrol-view.js") : null);
  const api = factory(view.escape);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusWorkspacePatrolPicker = api;
})(globalThis, function (escape) {
  "use strict";
  const icon = name => `<span class="ui-icon icon-${name}" aria-hidden="true"></span>`;
  const normalize = value => String(value || "").replace(/\\/g, "/").toLocaleLowerCase();

  function shell() {
    return `<section class="patrol-workspace-picker" aria-labelledby="patrolPickerTitle">
      <header class="patrol-picker-heading"><div><h1 id="patrolPickerTitle">选择工作区</h1><p>Patrol 将在这个工作区组织讨论与执行。</p></div>
      <button class="quiet-button" type="button" data-patrol-bind>${icon("plus")}绑定新文件夹</button></header>
      <p class="patrol-picker-error" data-patrol-binding-error role="alert" hidden></p>
      <div class="patrol-picker-columns"><section class="patrol-picker-browser" aria-label="工作区列表">
      <label class="patrol-picker-search">${icon("search")}<span class="visually-hidden">搜索工作区名称或路径</span>
      <input type="search" data-patrol-search placeholder="搜索名称或路径" autocomplete="off"></label>
      <div class="patrol-picker-list" data-patrol-workspaces role="region" aria-label="可选择的工作区" tabindex="0" aria-busy="true"><p class="patrol-picker-empty">正在读取工作区…</p></div>
      <p class="patrol-picker-count" data-patrol-workspace-count role="status"></p></section>
      <section class="patrol-picker-detail" data-patrol-workspace-detail aria-label="选中工作区详情"><div class="patrol-picker-empty">选择一个工作区，查看详情。</div></section></div></section>`;
  }

  function row(workspace) {
    const name = workspace.display_name || workspace.workspace_id;
    return `<li><button type="button" class="patrol-workspace-row" data-patrol-workspace-id="${escape(workspace.workspace_id)}" aria-pressed="false">
      ${icon("folder")}<span class="patrol-workspace-label"><strong>${escape(name)}</strong><span title="${escape(workspace.path)}">${escape(workspace.path)}</span></span>${icon("chevron-right")}</button></li>`;
  }

  function detail(workspace) {
    return `<div class="patrol-workspace-summary"><div class="patrol-workspace-emblem">${icon("folder")}</div>
      <h2>${escape(workspace.display_name || workspace.workspace_id)}</h2><div class="patrol-workspace-path">${escape(workspace.path)}</div>
      <p class="patrol-workspace-description">在这里持续输入想法和决定，Patrol 负责组织上下文与执行。</p>
      <button class="primary patrol-workspace-enter" type="button" data-patrol-enter>进入 Patrol</button>
      <p class="patrol-workspace-note">选择工作区不会启动执行，发送首条信息后开始。</p></div>`;
  }

  function create({ api, chooseFolder, onEnter, rememberedId = null, restoreRemembered = false }) {
    let page = null;
    let workspaces = [];
    let selectedId = null;
    let loading = true;
    const alive = () => Boolean(page?.isConnected);
    const selected = () => workspaces.find(item => item.workspace_id === selectedId);

    function showError(failure) {
      if (!alive()) return;
      const message = page.querySelector("[data-patrol-binding-error]");
      message.textContent = failure?.message || String(failure || "");
      message.hidden = !message.textContent;
    }

    function select(identity) {
      selectedId = identity;
      const workspace = selected();
      for (const button of page.querySelectorAll("[data-patrol-workspace-id]")) {
        button.setAttribute("aria-pressed", String(button.dataset.patrolWorkspaceId === identity));
      }
      page.querySelector("[data-patrol-workspace-detail]").innerHTML = workspace ? detail(workspace)
        : '<div class="patrol-picker-empty">选择一个工作区，查看详情。</div>';
    }

    function filter() {
      const query = normalize(page.querySelector("[data-patrol-search]").value.trim());
      const visible = workspaces.filter(item => normalize(item.display_name).includes(query) || normalize(item.path).includes(query));
      const list = page.querySelector("[data-patrol-workspaces]");
      list.setAttribute("aria-busy", String(loading));
      list.innerHTML = visible.length ? `<ul>${visible.map(row).join("")}</ul>`
        : `<p class="patrol-picker-empty">${loading ? "正在读取工作区…" : workspaces.length ? "没有匹配的工作区，试试其他名称或路径。" : "还没有工作区，绑定一个文件夹即可开始。"}</p>`;
      page.querySelector("[data-patrol-workspace-count]").textContent = loading ? "" : query
        ? `${visible.length} 个匹配工作区，共 ${workspaces.length} 个` : `${workspaces.length} 个工作区 · 可按名称或路径搜索`;
      list.scrollTop = 0;
      select(visible.some(item => item.workspace_id === selectedId) ? selectedId : visible[0]?.workspace_id || null);
    }

    async function bindFolder() {
      const owner = page;
      const button = owner.querySelector("[data-patrol-bind]");
      if (button.disabled) return;
      button.disabled = true;
      showError(null);
      try {
        const workspace = await chooseFolder();
        if (page === owner && alive() && workspace) onEnter(workspace);
      } catch (failure) {
        if (page === owner) showError(failure);
      } finally {
        if (page === owner && alive()) button.disabled = false;
      }
    }

    function click(event) {
      const button = event.target.closest("button");
      if (!button || !page.contains(button)) return;
      if (button.hasAttribute("data-patrol-bind")) void bindFolder();
      else if (button.dataset.patrolWorkspaceId) select(button.dataset.patrolWorkspaceId);
      else if (button.hasAttribute("data-patrol-enter") && selected()) onEnter(selected());
    }

    async function mount(host) {
      dispose();
      host.innerHTML = shell();
      page = host.firstElementChild;
      const owner = page;
      page.addEventListener("click", click);
      page.querySelector("[data-patrol-search]").addEventListener("input", filter);
      try {
        const loaded = await api.patrolWorkspaces();
        if (page !== owner || !alive()) return;
        workspaces = loaded;
        loading = false;
        const remembered = workspaces.find(item => item.workspace_id === rememberedId);
        if (remembered && restoreRemembered) return onEnter(remembered);
        selectedId = remembered?.workspace_id || workspaces[0]?.workspace_id || null;
        filter();
      } catch (failure) {
        if (page !== owner || !alive()) return;
        loading = false;
        filter();
        page.querySelector("[data-patrol-workspaces]").innerHTML = '<p class="patrol-picker-empty">读取工作区失败。仍可绑定新文件夹，或重新进入此页面重试。</p>';
        showError(failure);
      }
    }

    function dispose() {
      page?.removeEventListener("click", click);
      page?.querySelector("[data-patrol-search]").removeEventListener("input", filter);
      page = null;
      workspaces = [];
      selectedId = null;
      loading = true;
    }
    return Object.freeze({ mount, dispose });
  }
  return Object.freeze({ create });
});
