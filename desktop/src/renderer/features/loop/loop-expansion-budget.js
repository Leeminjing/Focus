/*
 * 本文件对外提供 Context Expansion 资源策略与用量的只读状态。
 * 输入为只读 Loop 资源快照；输出为逐资源真实用量、历史限制和阻断展示。
 * 具体工作流为按 API 快照展示冻结用量及真实阻断阶段和边界，不提供预算写入入口。
 * 示例：`FocusLoopExpansionBudget.renderStatus(resources)`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopExpansionBudget = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";
  // 源文案在渲染时取当前语言；模板插值始终保留原文。
  function localize(source, ...values) {
    if (globalThis.FocusI18n?.text) return globalThis.FocusI18n.text(source, ...values);
    return typeof source === "string" ? source : source.reduce((out, part, i) => out + part + (i < values.length ? String(values[i]) : ""), "");
  }


  const VERSION = "expansion-resource-policy-v1";
  // A null ceiling imposes no extra budget; null output reserve follows the model.
  const DEFAULTS = Object.freeze({
    version: VERSION,
    max_queries: null,
    max_unique_candidates: null,
    max_exact_reads: null,
    max_planner_model_calls: null,
    max_model_attempts_per_operation: null,
    max_planner_tokens: null,
    max_compiled_evidence_items: null,
    max_catalog_descriptor_chars: null,
    max_request_input_tokens: null,
    output_token_reserve: null,
  });
  const FIELDS = () => Object.freeze([
    ["max_queries", localize("检索查询")],
    ["max_unique_candidates", localize("唯一候选")],
    ["max_exact_reads", localize("精确读取")],
    ["max_planner_model_calls", localize("规划模型调用")],
    ["max_model_attempts_per_operation", localize("每步模型尝试")],
    ["max_planner_tokens", localize("规划 Token")],
    ["max_compiled_evidence_items", localize("编译证据项")],
    ["max_catalog_descriptor_chars", localize("目录字符")],
    ["max_request_input_tokens", localize("单请求输入 Token")],
    ["output_token_reserve", localize("输出预留 Token")],
  ]);
  const SESSION_USAGE = () => Object.freeze([
    ["queries", localize("检索查询"), "max_queries", "queries"],
    ["unique_candidates", localize("候选"), "max_candidates", "candidates"],
    ["exact_reads", localize("精读"), "max_exact_reads", "exact_reads"],
    ["planner_model_calls", localize("模型调用"), "max_model_calls", "model_calls"],
    ["planner_tokens", localize("规划 Token"), "max_tokens", "tokens"],
  ]);
  const escape = value => String(value ?? "").replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  function renderStatus(resources) {
    if (!resources?.effective) return "";
    const effective = resources.effective;
    const policy = effective.policy || DEFAULTS;
    const values = FIELDS().map(([key, label]) => `<span>${escape(label)} ${escape(policy[key] ?? (key === "output_token_reserve" ? localize("按模型配置") : localize("不限")))}</span>`).join("");
    return localize`<section class="loop-expansion-resource-status" aria-label="Context Expansion 资源"><h3>Context Expansion 资源</h3><p>策略 ${escape(policy.version)} · 来源 ${escape(effective.source)} · Grant R${escape(effective.grant_revision)}</p><div>${values}</div>${renderUsage(resources.latest_session)}${renderGlobalRemaining(effective)}${renderBlocker(resources.latest_session)}${renderRepeated(resources.repeated_blocker)}</section>`;
  }

  function renderUsage(session) {
    if (!session) return localize("<p>尚无规划 Session</p>");
    const counters = SESSION_USAGE().map(([key, label, limit, usedField]) => {
      const used = session.usage?.[usedField] ?? 0;
      const maximum = session.limits?.[limit];
      const remaining = maximum == null ? localize("不限") : session.remaining?.[key] ?? Math.max(0, maximum - used);
      return localize`${escape(label)} ${escape(used)} / ${escape(maximum ?? localize("不限"))}（剩余 ${escape(remaining)}）`;
    });
    return `<p>Session ${escape(session.session_id)} · ${escape(session.state)} · ${counters.join(" · ")}</p>`;
  }

  function renderGlobalRemaining(effective) {
    const display = value => value === null ? localize("不限") : value ?? "—";
    return localize`<p>全局剩余：模型调用 ${escape(display(effective.global_model_calls_remaining))} · 输入 Token ${escape(display(effective.global_input_tokens_remaining))} · 输出 Token ${escape(display(effective.global_output_tokens_remaining))}</p>`;
  }

  function renderBlocker(session) {
    if (!session?.blocker_code) return "";
    return localize`<p class="loop-expansion-blocker" role="status">阻断层：${escape(session.blocker_code)} · 阶段 ${escape(session.stage)} · 边界 ${escape(session.blocker_boundary || localize("历史记录未提供"))} · 操作 ${escape(session.blocker_operation_id || "—")} · ${escape(session.blocker_summary || localize("无安全摘要"))}</p>`;
  }

  function renderRepeated(repeated) {
    if (!repeated) return "";
    return localize`<p class="loop-expansion-blocker is-repeated" role="alert">连续 ${escape(repeated.consecutive_rounds)} 轮 Context Expansion 阻断：${escape(repeated.code)} · 边界 ${escape(repeated.boundary || localize("历史记录未提供"))} · ${escape(repeated.action)}</p>`;
  }

  return Object.freeze({ DEFAULTS, renderStatus });
});
