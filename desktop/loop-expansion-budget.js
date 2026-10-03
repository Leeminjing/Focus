/*
 * 本文件对外提供 Loop 和 Context Expansion 预算的默认值、字段渲染、表单读取与资源状态。
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
  const LOOP_FIELDS = Object.freeze([
    ["maxRounds", "max_rounds", "最大轮次", 1, 1000],
    ["maxDurationSeconds", "max_duration_seconds", "最长秒数", 60],
    ["maxModelCalls", "max_model_calls", "最大模型调用", 1],
    ["maxInputTokens", "max_input_tokens", "最大输入 Token", 1],
    ["maxOutputTokens", "max_output_tokens", "最大输出 Token", 1],
    ["maxRetries", "max_retries", "最大重试", 0],
    ["maxLanes", "max_lanes", "最大 Lane", 1, 64],
    ["maxContexts", "max_contexts", "最大 Context", 1, 256],
    ["maxProviders", "max_providers", "最大 Provider", 1, 32],
    ["maxNewLanesPerRound", "max_new_lanes_per_round", "每轮最大新 Lane", 0, 16],
    ["maxConcurrentRuns", "max_concurrent_runs", "最大并行 Run", 1, 32],
    ["maxNoProgress", "max_no_progress", "最大无进展轮次", 1, 20],
  ]);
  const LOOP_DEFAULTS = Object.freeze(Object.fromEntries(LOOP_FIELDS.map(([, key, , , max]) => [key, max ?? null])));
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

  function numberInput(name, label, value, min, max, placeholder = "不限") {
    return `<label>${escape(label)}<input name="${name}" type="number" min="${min}"${max == null ? ` placeholder="${placeholder}"` : ` max="${max}" required`} value="${escape(value)}"></label>`;
  }

  function readNumber(values, name, min, max) {
    const raw = values.get(name);
    if (raw !== null && String(raw).trim() === "" && max == null) return null;
    const value = Number(raw);
    if (raw === null || String(raw).trim() === "" || !Number.isSafeInteger(value) || value < min || (max != null && value > max)) throw new Error(`${name} 必须是有效整数`);
    return value;
  }

  function renderLoopInputs(budgets = LOOP_DEFAULTS) {
    const effective = { ...LOOP_DEFAULTS, ...budgets };
    return LOOP_FIELDS.map(([name, key, label, min, max]) => numberInput(name, label, effective[key], min, max)).join("") + renderInputs(effective.expansion_resources);
  }

  function readLoop(values, starting = false) {
    return {
      ...Object.fromEntries(LOOP_FIELDS.map(([name, key, , min, max]) => [key, readNumber(values, name, min, max)])),
      ...submission(values, starting),
    };
  }

  function renderInputs(policy = DEFAULTS) {
    const effective = { ...DEFAULTS, ...(policy || {}) };
    return `<fieldset class="loop-expansion-budget"><legend>Context Expansion 资源</legend><p>留空表示不限；单次请求受模型容量约束，输出预留留空时按模型配置。</p>${FIELDS.map(([key, label]) => numberInput(formName(key), label, effective[key], minimum(key), null, key === "output_token_reserve" ? "按模型配置" : "不限")).join("")}</fieldset>`;
  }

  function read(values) {
    const policy = { version: VERSION };
    for (const [key] of FIELDS) policy[key] = readNumber(values, formName(key), minimum(key));
    if (policy.max_unique_candidates !== null && policy.max_exact_reads !== null && policy.max_exact_reads > policy.max_unique_candidates) throw new Error("精确读取不能超过唯一候选容量");
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
    const values = FIELDS.map(([key, label]) => `<span>${escape(label)} ${escape(policy[key] ?? (key === "output_token_reserve" ? "按模型配置" : "不限"))}</span>`).join("");
    return `<section class="loop-expansion-resource-status" aria-label="Context Expansion 资源"><h3>Context Expansion 资源</h3><p>策略 ${escape(policy.version)} · 来源 ${escape(effective.source)} · Grant R${escape(effective.grant_revision)}</p><div>${values}</div>${renderUsage(resources.latest_session)}${renderGlobalRemaining(effective)}${renderBlocker(resources.latest_session)}${renderRepeated(resources.repeated_blocker)}</section>`;
  }

  function renderUsage(session) {
    if (!session) return "<p>尚无规划 Session</p>";
    const counters = SESSION_USAGE.map(([key, label, limit, usedField]) => {
      const used = session.usage?.[usedField] ?? 0;
      const maximum = session.limits?.[limit];
      const remaining = maximum == null ? "不限" : session.remaining?.[key] ?? Math.max(0, maximum - used);
      return `${escape(label)} ${escape(used)} / ${escape(maximum ?? "不限")}（剩余 ${escape(remaining)}）`;
    });
    return `<p>Session ${escape(session.session_id)} · ${escape(session.state)} · ${counters.join(" · ")}</p>`;
  }

  function renderGlobalRemaining(effective) {
    const display = value => value === null ? "不限" : value ?? "—";
    return `<p>全局剩余：模型调用 ${escape(display(effective.global_model_calls_remaining))} · 输入 Token ${escape(display(effective.global_input_tokens_remaining))} · 输出 Token ${escape(display(effective.global_output_tokens_remaining))}</p>`;
  }

  function renderBlocker(session) {
    if (!session?.blocker_code) return "";
    return `<p class="loop-expansion-blocker" role="status">阻断层：${escape(session.blocker_code)} · 阶段 ${escape(session.stage)} · 边界 ${escape(session.blocker_boundary || "历史记录未提供")} · 操作 ${escape(session.blocker_operation_id || "—")} · ${escape(session.blocker_summary || "无安全摘要")}</p>`;
  }

  function renderRepeated(repeated) {
    if (!repeated) return "";
    return `<p class="loop-expansion-blocker is-repeated" role="alert">连续 ${escape(repeated.consecutive_rounds)} 轮 Context Expansion 阻断：${escape(repeated.code)} · 边界 ${escape(repeated.boundary || "历史记录未提供")} · ${escape(repeated.action)}</p>`;
  }

  return Object.freeze({ DEFAULTS, LOOP_DEFAULTS, renderLoopInputs, readLoop, renderInputs, read, submission, renderStatus });
});
