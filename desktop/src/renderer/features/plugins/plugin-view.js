/*
 * 本文件对外提供插件中心的纯渲染器。输入为插件清单、接口注册表、执行轨迹与本地筛选/选中状态，
 * 输出为真实插件卡片/列表和按需详情 HTML；工作流在完整已加载清单中检索/筛选，呈现解析结果，不改变注入顺序和生命周期。示例：FocusPluginView.render(plugins, interfaces, traces, { query: "file" })。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusPluginView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";
  // 源文案在渲染时取当前语言；模板插值始终保留原文。
  function localize(source, ...values) {
    if (globalThis.FocusI18n?.text) return globalThis.FocusI18n.text(source, ...values);
    return typeof source === "string" ? source : source.reduce((out, part, i) => out + part + (i < values.length ? String(values[i]) : ""), "");
  }


  function escapeHtml(value = "") {
    return String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  }

  const STATUS_BADGES = {
    get active() { return localize("已注册"); },
    get unavailable() { return localize("不可用"); },
    get rejected() { return localize("已拒绝"); },
    get pending() { return localize("待解析"); },
  };

  const STATUS_TONES = {
    active: "neutral",
    unavailable: "warning",
    rejected: "danger",
    pending: "neutral",
  };

  // Activation describes the server's lifecycle policy, not proof of live capabilities.
  function activationTiming(value) {
    const labels = {
      immediate: localize("加载后更新"),
      next_run: localize("下次 Run 装配时生效"),
      process_restart: localize("重启后端进程后生效"),
    };
    return labels[value] || localize("生效时机未知");
  }

  function renderActivation(activation = {}) {
    const rows = [
      ["registry", localize("注册表变更")],
      ["tools_and_hooks", localize("工具与 hooks 变更")],
      ["desktop_assets", localize("界面资源变更")],
      ["http_routes", localize("API 路由变更")],
    ];
    const running = activation.running_runs === "unchanged"
      ? localize("正在运行的 Run 保留原快照") : localize("运行中 Run 的生效范围未知");
    return `<table class="plugin-activation" aria-label="${localize("变更生效时机")}">
      <thead><tr><th scope="col">${localize("变更范围")}</th><th scope="col">${localize("生效时机")}</th></tr></thead>
      <tbody>${rows.map(([key, label]) => `<tr data-plugin-activation="${key}"><th scope="row">${label}</th><td>${activationTiming(activation[key])}${key === "tools_and_hooks" ? `<small>${running}</small>` : ""}</td></tr>`).join("")}</tbody>
      ${[activation.desktop_assets, activation.http_routes].includes("process_restart") ? `<tfoot><tr><td colspan="2">${localize("界面与 API 的新增、变更、禁用或卸载：桌面端请退出并重新启动 Focus。")}</td></tr></tfoot>` : ""}
    </table>`;
  }

  // 接口视图：编号顺序 + 只读标注（Read-only extension point）
  function renderInterfaces(interfaces, activation = {}) {
    const rows = Object.entries(interfaces || {}).map(([name, entry]) => {
      const ordered = (entry.plugins || []).map((plugin, index) => `${index + 1}. ${escapeHtml(plugin)}`).join("　");
      const tags = [
        entry.read_only ? `<span class="plugin-readonly-tag">Read-only extension point</span>` : "",
        entry.cardinality === "single" ? `<span class="plugin-cardinality-tag">single</span>` : "",
      ].join("");
      return `<article class="plugin-interface-row">
        <strong>${escapeHtml(name)}</strong>
        <span class="plugin-interface-order">${ordered || localize("（无实现）")}</span>
        ${tags}
        ${name === "tool" || name.startsWith("hook.") ? `<span class="plugin-interface-timing">${activationTiming(activation.tools_and_hooks)}</span>` : ""}
      </article>`;
    }).join("");
    return rows || localize`<p class="muted">暂无已注入接口</p>`;
  }

  function renderPluginCard(plugin, selected = false) {
    const injected = (plugin.injected || []).map(name => `<span class="plugin-injected-tag">${escapeHtml(name)}</span>`).join("") || localize("（无）");
    const missing = plugin.missing?.length
      ? `<span class="plugin-reason">Missing dependency: ${escapeHtml(plugin.missing.join(", "))}</span>`
      : "";
    const conflict = plugin.conflict
      ? `<span class="plugin-reason danger">Injection Conflict — Interface: ${escapeHtml(plugin.conflict.interface)} · Current: ${escapeHtml(plugin.conflict.current)} · New: ${escapeHtml(plugin.conflict.new)}</span>`
      : "";
    const reason = !missing && !conflict && plugin.reason ? `<span class="plugin-reason">${escapeHtml(plugin.reason)}</span>` : "";
    return `<button type="button" class="plugin-card status-${escapeHtml(plugin.status)}${selected ? " is-selected" : ""}" data-action="select-plugin" data-plugin-name="${escapeHtml(plugin.name)}" aria-pressed="${selected}">
      <span class="plugin-card-heading"><span><strong>${escapeHtml(plugin.name)}</strong><small>v${escapeHtml(plugin.version)}</small></span><span class="ui-badge is-${STATUS_TONES[plugin.status] || "neutral"}"><span class="visually-hidden">${localize("注册状态：")}</span>${STATUS_BADGES[plugin.status] || escapeHtml(plugin.status)}</span></span>
      <span class="plugin-card-capabilities">${injected}</span>
      ${missing}${conflict}${reason}
    </button>`;
  }

  function renderTraces(traces) {
    if (!traces?.length) return localize`<p class="muted">暂无执行轨迹（插件 hook 参与执行后在此展示）</p>`;
    return localize`<table class="plugin-trace-table">
      <thead><tr><th>接口</th><th>插件</th><th>状态</th><th>耗时</th><th>详情</th></tr></thead>
      <tbody>${traces.map(trace => `<tr class="trace-${escapeHtml(trace.status)}">
        <td><code>${escapeHtml(trace.interface)}</code></td>
        <td>${escapeHtml(trace.plugin)}</td>
        <td>${escapeHtml(trace.status)}</td>
        <td>${trace.duration_ms ?? ""} ms</td>
        <td class="plugin-trace-error">${escapeHtml(trace.error || "")}</td>
      </tr>`).join("")}</tbody>
    </table>`;
  }

  function renderPluginDetail(plugin, interfaces, traces, activation = {}) {
    if (!plugin) return localize`<section class="ui-empty-state"><h1>没有匹配的插件</h1><p>切换状态筛选查看其他插件。</p></section>`;
    const relevantInterfaces = Object.fromEntries(Object.entries(interfaces || {}).filter(([, entry]) => (entry.plugins || []).includes(plugin.name)));
    const relevantTraces = (traces || []).filter(trace => trace.plugin === plugin.name);
    const errors = relevantTraces.filter(trace => !["success", "completed"].includes(trace.status));
    const missing = plugin.missing || [];
    return localize`<section class="plugin-detail status-${escapeHtml(plugin.status)}">
      ${plugin.conflict ? localize`<div class="ui-notice is-danger"><strong>接口注入冲突</strong><span>${escapeHtml(plugin.conflict.interface)} 已由 ${escapeHtml(plugin.conflict.current)} 注册，无法再注入 ${escapeHtml(plugin.conflict.new)}。</span></div>` : ""}
      ${missing.length ? localize`<div class="ui-notice is-warning"><strong>缺少依赖</strong><span>${escapeHtml(missing.join(", "))}</span></div>` : ""}
      ${plugin.reason && !plugin.conflict && !missing.length ? localize`<div class="ui-notice is-warning"><strong>插件不可用</strong><span>${escapeHtml(plugin.reason)}</span></div>` : ""}
      <section class="plugin-detail-section"><header><h3>接口注入</h3><span>${Object.keys(relevantInterfaces).length} 个</span></header>${renderInterfaces(relevantInterfaces, activation)}</section>
      <section class="plugin-detail-section"><header><h3>依赖</h3><span>${(plugin.requires || []).length} 个</span></header><div class="plugin-dependency-list">${(plugin.requires || []).map(name => `<span class="ui-badge">${escapeHtml(name)}</span>`).join("") || localize('<span class="muted">无外部插件依赖</span>')}</div></section>
      <section class="plugin-detail-section plugin-traces"><header><h3>执行轨迹</h3><span>${errors.length ? localize`${errors.length} 条异常优先` : localize`最近 ${relevantTraces.length} 条`}</span></header>${renderTraces([...errors, ...relevantTraces.filter(trace => !errors.includes(trace))])}</section>
    </section>`;
  }

  // 只展示接口交互（哪个接口被调用、哪些插件参与、成败），不展示插件内部技术实现
  function render(plugins, interfaces, traces, options = {}) {
    const all = plugins || [];
    const filter = options.filter || "all";
    const query = String(options.query || "").trim().toLowerCase();
    const filtered = all.filter(plugin => (filter === "all" || plugin.status === filter) && (!query || `${plugin.name} ${(plugin.injected || []).join(" ")}`.toLowerCase().includes(query)));
    const selected = filtered.find(plugin => plugin.name === options.selectedName) || null;
    const busy = Boolean(options.loading || options.reloading);
    const refreshStatus = options.loading ? localize("正在查询插件状态…") : options.error ? localize("状态查询未完成") : options.queried ? localize("状态已刷新") : localize("仅读取当前状态");
    const reloadStatus = options.reloading ? localize("正在加载插件变更…") : options.reloadError ? localize("请刷新状态核对") : options.reloadSucceeded ? localize("已加载到注册表") : localize("更新插件注册表");
    const filters = [
      ["all", localize("全部")], ["active", localize("已注册")], ["unavailable", localize("不可用")], ["rejected", localize("已拒绝")],
    ].map(([status, label]) => {
      const count = status === "all" ? all.length : all.filter(plugin => plugin.status === status).length;
      return `<button type="button" class="${filter === status ? "active" : ""}" data-action="filter-plugins" data-plugin-status="${status}" aria-pressed="${filter === status}">${label} <span>${count}</span></button>`;
    }).join("");
    return localize`<section class="plugins-view">
      <header class="plugins-heading">
        <h1>插件</h1>
        <div class="plugin-operations">
          <div class="plugin-operation"><button class="text-button" data-action="refresh-plugins" aria-disabled="${busy}" aria-busy="${Boolean(options.loading)}" aria-describedby="pluginQueryStatus">刷新状态</button><p id="pluginQueryStatus" role="status" aria-atomic="true">${refreshStatus}</p>
            ${options.error ? localize`<p role="alert">刷新状态失败：${escapeHtml(options.error)}</p>` : ""}</div>
          <div class="plugin-operation"><button class="quiet-button" data-action="reload-plugins" aria-disabled="${busy}" aria-busy="${Boolean(options.reloading)}" aria-describedby="pluginReloadStatus">加载插件变更</button><p id="pluginReloadStatus" role="status" aria-atomic="true">${reloadStatus}</p>
            ${options.reloadError ? localize`<p role="alert">加载结果未确认：${escapeHtml(options.reloadError)}</p>` : ""}</div>
        </div>
      </header>
      ${renderActivation(options.activation)}
      <div class="library-toolbar"><div class="plugin-filter segmented" role="group" aria-label="按插件状态筛选">${filters}</div>
      <label class="page-search">检索已加载插件 <input type="search" data-page-search="plugins" value="${escapeHtml(options.query || "")}" placeholder="查找能力…"></label><div class="library-layout" role="group" aria-label="插件布局" data-i18n-aria-label="view.layout">${["cards", "list"].map(layout => `<button data-action="set-library-layout" data-library="plugins" data-layout="${layout}" aria-pressed="${(options.layout || "cards") === layout}" data-i18n="view.${layout}">${layout === "cards" ? localize("卡片") : localize("列表")}</button>`).join("")}</div></div>
      <div class="plugins-workbench">
        <aside class="plugins-list${options.layout === "list" ? " is-list" : ""}"><header><strong>插件</strong><span>${filtered.length}/${all.length}</span></header><div class="plugin-list-scroll">${filtered.map(plugin => renderPluginCard(plugin, plugin === selected)).join("") || `<section class="ui-empty-state"><h1>${all.length ? localize("没有匹配项") : localize("尚无插件")}</h1><p>${all.length ? localize("切换筛选查看其他状态。") : localize("plugins/ 目录为空，宿主仍可独立启动。")}</p></section>`}</div></aside>
        ${selected ? localize`<details class="plugins-detail"${options.selectedName ? " open" : ""}><summary>接口、依赖与执行轨迹 · ${escapeHtml(selected.name)}</summary>${renderPluginDetail(selected, interfaces, traces, options.activation)}</details>` : ""}
      </div>
    </section>`;
  }

  return { render, renderInterfaces, renderPluginCard, renderPluginDetail, renderTraces, renderActivation };
});
