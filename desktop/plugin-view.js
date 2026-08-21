/*
 * 本文件对外提供插件中心的纯渲染器。输入为插件清单、接口注册表、执行轨迹与本地筛选/选中状态，
 * 输出为主从插件工作台 HTML；工作流只呈现插件解析结果，不改变注入顺序和插件生命周期。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusPluginView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function escapeHtml(value = "") {
    return String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  }

  const STATUS_BADGES = {
    active: "已生效",
    unavailable: "不可用",
    rejected: "已拒绝",
    pending: "待解析",
  };

  const STATUS_TONES = {
    active: "success",
    unavailable: "warning",
    rejected: "danger",
    pending: "neutral",
  };

  // 接口视图：编号顺序 + 只读标注（Read-only extension point）
  function renderInterfaces(interfaces) {
    const rows = Object.entries(interfaces || {}).map(([name, entry]) => {
      const ordered = (entry.plugins || []).map((plugin, index) => `${index + 1}. ${escapeHtml(plugin)}`).join("　");
      const tags = [
        entry.read_only ? `<span class="plugin-readonly-tag">Read-only extension point</span>` : "",
        entry.cardinality === "single" ? `<span class="plugin-cardinality-tag">single</span>` : "",
      ].join("");
      return `<article class="plugin-interface-row">
        <strong>${escapeHtml(name)}</strong>
        <span class="plugin-interface-order">${ordered || "（无实现）"}</span>
        ${tags}
      </article>`;
    }).join("");
    return rows || `<p class="muted">暂无已注入接口</p>`;
  }

  function renderPluginCard(plugin, selected = false) {
    const injected = (plugin.injected || []).map(name => `<span class="plugin-injected-tag">${escapeHtml(name)}</span>`).join("") || "（无）";
    const missing = plugin.missing?.length
      ? `<span class="plugin-reason">Missing dependency: ${escapeHtml(plugin.missing.join(", "))}</span>`
      : "";
    const conflict = plugin.conflict
      ? `<span class="plugin-reason danger">Injection Conflict — Interface: ${escapeHtml(plugin.conflict.interface)} · Current: ${escapeHtml(plugin.conflict.current)} · New: ${escapeHtml(plugin.conflict.new)}</span>`
      : "";
    const reason = !missing && !conflict && plugin.reason ? `<span class="plugin-reason">${escapeHtml(plugin.reason)}</span>` : "";
    return `<button type="button" class="plugin-card status-${escapeHtml(plugin.status)}${selected ? " is-selected" : ""}" data-action="select-plugin" data-plugin-name="${escapeHtml(plugin.name)}" aria-pressed="${selected}">
      <span class="plugin-card-heading"><span><strong>${escapeHtml(plugin.name)}</strong><small>v${escapeHtml(plugin.version)}</small></span><span class="ui-badge is-${STATUS_TONES[plugin.status] || "neutral"}">${STATUS_BADGES[plugin.status] || escapeHtml(plugin.status)}</span></span>
      <span class="plugin-card-capabilities">${injected}</span>
      ${missing}${conflict}${reason}
    </button>`;
  }

  function renderTraces(traces) {
    if (!traces?.length) return `<p class="muted">暂无执行轨迹（插件 hook 参与执行后在此展示）</p>`;
    return `<table class="plugin-trace-table">
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

  function renderPluginDetail(plugin, interfaces, traces) {
    if (!plugin) return `<section class="ui-empty-state"><h1>没有匹配的插件</h1><p>切换状态筛选查看其他插件。</p></section>`;
    const relevantInterfaces = Object.fromEntries(Object.entries(interfaces || {}).filter(([, entry]) => (entry.plugins || []).includes(plugin.name)));
    const relevantTraces = (traces || []).filter(trace => trace.plugin === plugin.name);
    const errors = relevantTraces.filter(trace => !["success", "completed"].includes(trace.status));
    const missing = plugin.missing || [];
    return `<section class="plugin-detail status-${escapeHtml(plugin.status)}">
      <header class="plugin-detail-heading">
        <div><span class="workspace-kicker">PLUGIN DETAIL</span><h2>${escapeHtml(plugin.name)}</h2><p>版本 ${escapeHtml(plugin.version)} · ${STATUS_BADGES[plugin.status] || escapeHtml(plugin.status)}</p></div>
        <span class="ui-badge is-${STATUS_TONES[plugin.status] || "neutral"}">${STATUS_BADGES[plugin.status] || escapeHtml(plugin.status)}</span>
      </header>
      ${plugin.conflict ? `<div class="ui-notice is-danger"><strong>接口注入冲突</strong><span>${escapeHtml(plugin.conflict.interface)} 已由 ${escapeHtml(plugin.conflict.current)} 注册，无法再注入 ${escapeHtml(plugin.conflict.new)}。</span></div>` : ""}
      ${missing.length ? `<div class="ui-notice is-warning"><strong>缺少依赖</strong><span>${escapeHtml(missing.join(", "))}</span></div>` : ""}
      ${plugin.reason && !plugin.conflict && !missing.length ? `<div class="ui-notice is-warning"><strong>插件不可用</strong><span>${escapeHtml(plugin.reason)}</span></div>` : ""}
      <section class="plugin-detail-section"><header><h3>接口注入</h3><span>${Object.keys(relevantInterfaces).length} 个</span></header>${renderInterfaces(relevantInterfaces)}</section>
      <section class="plugin-detail-section"><header><h3>依赖</h3><span>${(plugin.requires || []).length} 个</span></header><div class="plugin-dependency-list">${(plugin.requires || []).map(name => `<span class="ui-badge">${escapeHtml(name)}</span>`).join("") || '<span class="muted">无外部插件依赖</span>'}</div></section>
      <section class="plugin-detail-section plugin-traces"><header><h3>执行轨迹</h3><span>${errors.length ? `${errors.length} 条异常优先` : `最近 ${relevantTraces.length} 条`}</span></header>${renderTraces([...errors, ...relevantTraces.filter(trace => !errors.includes(trace))])}</section>
    </section>`;
  }

  // 只展示接口交互（哪个接口被调用、哪些插件参与、成败），不展示插件内部技术实现
  function render(plugins, interfaces, traces, options = {}) {
    const all = plugins || [];
    const filter = options.filter || "all";
    const filtered = all.filter(plugin => filter === "all" || plugin.status === filter);
    const selected = filtered.find(plugin => plugin.name === options.selectedName) || filtered[0] || null;
    const filters = [
      ["all", "全部"], ["active", "已生效"], ["unavailable", "不可用"], ["rejected", "已拒绝"],
    ].map(([status, label]) => {
      const count = status === "all" ? all.length : all.filter(plugin => plugin.status === status).length;
      return `<button type="button" class="${filter === status ? "active" : ""}" data-action="filter-plugins" data-plugin-status="${status}" aria-pressed="${filter === status}">${label} <span>${count}</span></button>`;
    }).join("");
    return `<section class="plugins-view">
      <header class="plugins-heading">
        <div><strong>插件运行状况</strong><p class="muted">检查能力注入、依赖、冲突与实际执行轨迹。</p></div>
        <button class="text-button" data-action="refresh-plugins">刷新</button>
      </header>
      <div class="plugin-filter segmented" role="group" aria-label="按插件状态筛选">${filters}</div>
      <div class="plugins-workbench">
        <aside class="plugins-list"><header><strong>插件</strong><span>${filtered.length}/${all.length}</span></header><div class="plugin-list-scroll">${filtered.map(plugin => renderPluginCard(plugin, plugin === selected)).join("") || `<section class="ui-empty-state"><h1>${all.length ? "没有匹配项" : "尚无插件"}</h1><p>${all.length ? "切换筛选查看其他状态。" : "plugins/ 目录为空，宿主仍可独立启动。"}</p></section>`}</div></aside>
        <main class="plugins-detail">${renderPluginDetail(selected, interfaces, traces)}</main>
      </div>
    </section>`;
  }

  return { render, renderInterfaces, renderPluginCard, renderPluginDetail, renderTraces };
});
