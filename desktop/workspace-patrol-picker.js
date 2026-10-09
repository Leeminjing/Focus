/* 本文件对外提供 FocusWorkspacePatrolPicker.create 的工作区选择页面。
 * 输入为系统文件夹选择回调和显式进入回调；输出为用户选中路径的确认页面。
 * 工作流为打开原生文件夹选择器，选择仅更新本页路径，确认进入才调用原绑定用例；取消保留原选择，
 * 离开后忽略迟到响应。页面不读取已登记目录，不创建 Context/Run；图标与转义复用宿主资产和 Patrol view。
 * 示例：const picker = FocusWorkspacePatrolPicker.create({ chooseFolder, onEnter }); picker.mount(host)。
 */
(function (root, factory) {
  const view = root.FocusWorkspacePatrolView || (typeof require === "function" ? require("./workspace-patrol-view.js") : null);
  const api = factory(view.escape);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusWorkspacePatrolPicker = api;
})(globalThis, function (escape) {
  "use strict";
  const icon = name => `<span class="ui-icon icon-${name}" aria-hidden="true"></span>`;

  function shell() {
    return `<section class="patrol-workspace-picker" aria-labelledby="patrolPickerTitle">
      <header class="patrol-picker-heading"><h1 id="patrolPickerTitle">选择工作区文件夹</h1><p>从电脑文件系统选择项目文件夹，Patrol 将在其中组织讨论与执行。</p></header>
      <p class="patrol-picker-error" data-patrol-binding-error role="alert" hidden></p>
      <section class="patrol-picker-detail" data-patrol-workspace-detail aria-label="工作区路径确认">${detail(null)}</section></section>`;
  }

  function detail(path) {
    const name = path ? path.split(/[\\/]/).filter(Boolean).at(-1) || path : "选择一个本地文件夹";
    return `<div class="patrol-workspace-summary"><div class="patrol-workspace-emblem">${icon("folder")}</div>
      <h2>${escape(name)}</h2>${path ? `<div class="patrol-workspace-path">${escape(path)}</div>` : ""}
      <p class="patrol-workspace-description">${path ? "确认这个文件夹后，即可进入 Patrol 输入想法和决定。" : "打开系统文件夹选择器，选择你要推进的项目目录。"}</p>
      <div class="patrol-picker-actions">${path ? '<button class="primary patrol-workspace-enter" type="button" data-patrol-enter>进入 Patrol</button>' : ""}
      <button class="${path ? "quiet-button" : "primary patrol-workspace-enter"}" type="button" data-patrol-bind>${icon("folder")}${path ? "重新选择文件夹" : "选择文件夹"}</button></div>
      <p class="patrol-workspace-note">选择工作区不会启动执行，发送首条信息后开始。</p></div>`;
  }

  function create({ chooseFolder, onEnter }) {
    let page = null;
    let path = null;
    const alive = () => Boolean(page?.isConnected);

    function showError(failure) {
      if (!alive()) return;
      const message = page.querySelector("[data-patrol-binding-error]");
      message.textContent = failure?.message || String(failure || "");
      message.hidden = !message.textContent;
    }

    async function act(button) {
      const owner = page;
      if (button.disabled) return;
      for (const control of page.querySelectorAll("button")) control.disabled = true;
      showError(null);
      try {
        if (button.hasAttribute("data-patrol-enter")) await onEnter(path);
        else {
          const chosen = await chooseFolder();
          if (page === owner && alive() && chosen) {
            path = chosen;
            page.querySelector("[data-patrol-workspace-detail]").innerHTML = detail(path);
            page.querySelector("[data-patrol-enter]").focus();
          }
        }
      } catch (failure) {
        if (page === owner) showError(failure);
      } finally {
        if (page === owner && alive()) for (const control of page.querySelectorAll("button")) control.disabled = false;
      }
    }

    function click(event) {
      const button = event.target.closest("button");
      if (!button || !page.contains(button)) return;
      if (button.hasAttribute("data-patrol-bind") || (button.hasAttribute("data-patrol-enter") && path)) void act(button);
    }

    function mount(host) {
      dispose();
      host.innerHTML = shell();
      page = host.firstElementChild;
      page.addEventListener("click", click);
    }

    function dispose() {
      page?.removeEventListener("click", click);
      page = null;
      path = null;
    }
    return Object.freeze({ mount, dispose });
  }
  return Object.freeze({ create });
});
