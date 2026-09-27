/*
 * 本文件对外提供 Context Expansion 预算的 DEFAULTS、renderInputs、read、submission 与 renderStatus。
 * 输入为有效版本化资源策略、表单值或只读 Loop 资源快照；输出为可编辑字段、严格数值 payload 与安全的逐资源用量/阻断展示。
 * 具体工作流为启动和修订共用字段映射，提交时构建嵌套策略，运行时按同一 API 快照展示冻结用量、剩余量及真实阻断阶段和边界。
 * 示例：`const resources = FocusLoopExpansionBudget.read(new FormData(form))`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopExpansionBudget = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const VERSION = "expansion-resource-policy-v1";
  const DEFAULTS = Object.freeze({
    version: VERSION,
    max_queries: 16,
    max_unique_candidates: 4096,
    max_exact_reads: 512,
    max_planner_model_calls: 32,
    max_model_attempts_per_operation: 3,
    max_planner_tokens: 4000000,
    max_compiled_evidence_items: 1024,
    max_catalog_descriptor_chars: 4000000,
    max_request_input_tokens: 96000,
    output_token_reserve: 8192,
  });
  const FIELDS = Object.freeze([
    ["max_queries", "检索查询"],
    ["max_unique_candidates", "唯一候选"],
    ["max_exact_reads", "精确读取"],
    ["max_planner_model_calls", "规划模型调用"],
    ["max_model_attempts_per_operation", "每步模型尝试"],
    ["max_planner_tokens", "规划 Token"],
    ["max_compiled_evidence_items", "编译证据项"],
    ["max_catalog_descriptor_chars", "目录字符"],
    ["max_request_input_tokens", "单请求输入 Token"],
    ["output_token_reserve", "输出预留 Token"],
  ]);
  const SESSION_USAGE = Object.freeze([
    ["queries", "检索查询", "max_queries", "queries"],
    ["unique_candidates", "候选", "max_candidates", "candidates"],
    ["exact_reads", "精读", "max_exact_reads", "exact_reads"],
    ["planner_model_calls", "模型调用", "max_model_calls", "model_calls"],
    ["planner_tokens", "规划 Token", "max_tokens", "tokens"],
  ]);
  const escape = value => String(value ?? "").replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  const formName = key => `expansion_${key}`;
  const minimum = key => key === "max_request_input_tokens" ? 1024 : key === "output_token_reserve" ? 512 : key === "max_catalog_descriptor_chars" || key === "max_model_attempts_per_operation" ? 1 : 0;

  function renderInputs(policy = DEFAULTS) {
    const effective = { ...DEFAULTS, ...(policy || {}) };
    return `<fieldset class="loop-expansion-budget"><legend>Context Expansion 资源</legend><p>默认容量面向长程大任务；更严格的全局授权仍然生效。</p>${FIELDS.map(([key, label]) => `<label>${escape(label)}<input name="${formName(key)}" type="number" min="${minimum(key)}" value="${escape(effective[key])}" required></label>`).join("")}</fieldset>`;
  }

  function read(values) {
    const policy = { version: VERSION };
    for (const [key] of FIELDS) {
      const raw = values.get(formName(key));
      const number = Number(raw);
      if (raw === null || String(raw).trim() === "" || !Number.isSafeInteger(number) || number < minimum(key)) throw new Error(`Context Expansion ${key} 必须是有效整数`);
      policy[key] = number;
    }
    if (policy.max_exact_reads > policy.max_unique_candidates) throw new Error("精确读取不能超过唯一候选容量");
    return policy;
  }

  function submission(values, starting = false) {
    const resources = read(values);
    return starting && Object.keys(resources).every(key => resources[key] === DEFAULTS[key])
      ? {}
      : { expansion_resources: resources };
  }

  function renderStatus(resources) {
    if (!resources?.effective) return "";
    const effective = resources.effective;
    const policy = effective.policy || DEFAULTS;
    const values = FIELDS.map(([key, label]) => `<span>${escape(label)} ${escape(policy[key])}</span>`).join("");
    return `<section class="loop-expansion-resource-status" aria-label="Context Expansion 资源"><h3>Context Expansion 资源</h3><p>策略 ${escape(policy.version)} · 来源 ${escape(effective.source)} · Grant R${escape(effective.grant_revision)}</p><div>${values}</div>${renderUsage(resources.latest_session)}${renderGlobalRemaining(effective)}${renderBlocker(resources.latest_session)}${renderRepeated(resources.repeated_blocker)}</section>`;
  }

  function renderUsage(session) {
    if (!session) return "<p>尚无规划 Session</p>";
    const counters = SESSION_USAGE.map(([key, label, limit, usedField]) => {
      const used = session.usage?.[usedField] ?? 0;
      const maximum = session.limits?.[limit] ?? 0;
      const remaining = session.remaining?.[key] ?? Math.max(0, maximum - used);
      return `${escape(label)} ${escape(used)} / ${escape(maximum)}（剩余 ${escape(remaining)}）`;
    });
    return `<p>Session ${escape(session.session_id)} · ${escape(session.state)} · ${counters.join(" · ")}</p>`;
  }

  function renderGlobalRemaining(effective) {
    return `<p>全局剩余：模型调用 ${escape(effective.global_model_calls_remaining ?? "—")} · 输入 Token ${escape(effective.global_input_tokens_remaining ?? "—")} · 输出 Token ${escape(effective.global_output_tokens_remaining ?? "—")}</p>`;
  }

  function renderBlocker(session) {
    if (!session?.blocker_code) return "";
    return `<p class="loop-expansion-blocker" role="status">阻断层：${escape(session.blocker_code)} · 阶段 ${escape(session.stage)} · 边界 ${escape(session.blocker_boundary || "历史记录未提供")} · 操作 ${escape(session.blocker_operation_id || "—")} · ${escape(session.blocker_summary || "无安全摘要")}</p>`;
  }

  function renderRepeated(repeated) {
    if (!repeated) return "";
    return `<p class="loop-expansion-blocker is-repeated" role="alert">连续 ${escape(repeated.consecutive_rounds)} 轮 Context Expansion 阻断：${escape(repeated.code)} · 边界 ${escape(repeated.boundary || "历史记录未提供")} · ${escape(repeated.action)}</p>`;
  }

  return Object.freeze({ DEFAULTS, renderInputs, read, submission, renderStatus });
});
