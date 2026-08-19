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

  function renderPluginCard(plugin) {
    const injected = (plugin.injected || []).map(name => `<span class="plugin-injected-tag">${escapeHtml(name)}</span>`).join("") || "（无）";
    const missing = plugin.missing?.length
      ? `<p class="plugin-reason">Missing dependency: ${escapeHtml(plugin.missing.join(", "))}</p>`
      : "";
    const conflict = plugin.conflict
      ? `<p class="plugin-reason danger">Injection Conflict — Interface: ${escapeHtml(plugin.conflict.interface)} · Current: ${escapeHtml(plugin.conflict.current)} · New: ${escapeHtml(plugin.conflict.new)}</p>`
      : "";
    const reason = !missing && !conflict && plugin.reason ? `<p class="plugin-reason">${escapeHtml(plugin.reason)}</p>` : "";
    return `<article class="plugin-card status-${escapeHtml(plugin.status)}">
      <header>
        <div><strong>${escapeHtml(plugin.name)}</strong><span class="muted tiny">v${escapeHtml(plugin.version)}</span></div>
        <span class="plugin-status-badge">${STATUS_BADGES[plugin.status] || plugin.status}</span>
      </header>
      <p><span class="muted tiny">Injected:</span> ${injected}</p>
      <p><span class="muted tiny">Dependencies:</span> ${escapeHtml((plugin.requires || []).join(", ") || "—")}</p>
      ${missing}${conflict}${reason}
    </article>`;
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

  // 只展示接口交互（哪个接口被调用、哪些插件参与、成败），不展示插件内部技术实现
  function render(plugins, interfaces, traces) {
    return `<section class="plugins-view">
      <header class="plugins-heading">
        <div><span class="review-kicker">PLUGINS</span><h1>插件</h1><p class="muted">插件接入：声明接口实现 → 校验 → 注入 → 已有能力集合增加</p></div>
        <button class="text-button" data-action="refresh-plugins">刷新</button>
      </header>
      <div class="plugins-panels">
        <section class="plugins-list">
          <header class="compression-panel-heading"><strong>插件清单</strong><span>${(plugins || []).length} 个</span></header>
          ${(plugins || []).map(renderPluginCard).join("") || `<p class="muted">plugins/ 目录为空，尚无插件</p>`}
        </section>
        <section class="plugins-interfaces">
          <header class="compression-panel-heading"><strong>接口实现</strong><span>稳定顺序</span></header>
          ${renderInterfaces(interfaces)}
          <header class="compression-panel-heading"><strong>执行轨迹</strong><span>最近 ${(traces || []).length} 条</span></header>
          ${renderTraces(traces)}
        </section>
      </div>
    </section>`;
  }

  return { render, renderInterfaces, renderPluginCard, renderTraces };
});
