/* 普通会话的模型选择、窗口估算与单轮用量展示。配置由 task.ui_state 保存，发送时冻结。 */
(function (root) {
  // 源文案在渲染时取当前语言；模板插值始终保留原文。
  function localize(source, ...values) {
    if (globalThis.FocusI18n?.text) return globalThis.FocusI18n.text(source, ...values);
    return typeof source === "string" ? source : source.reduce((out, part, i) => out + part + (i < values.length ? String(values[i]) : ""), "");
  }

  const labels = { get none() { return localize("关闭"); }, get minimal() { return localize("最低"); }, get low() { return localize("低"); }, get medium() { return localize("中"); }, get high() { return localize("高"); }, get xhigh() { return localize("更高"); }, get max() { return localize("最高"); }, get ultra() { return localize("极高"); } };
  function model(equipment, saved = {}) {
    const name = saved.session_model_name || equipment.default_model_name;
    return equipment.models.find(item => item.name === name) || null;
  }
  function request(equipment, saved = {}) {
    const selected = model(equipment, saved);
    const name = saved.session_model_name || selected?.name;
    const effort = saved.session_reasoning_effort;
    return { ...(name ? { model_name: name } : {}),
      ...(effort && selected?.reasoning_efforts?.includes(effort) ? { reasoning_effort: effort } : {}) };
  }
  function usage(equipment, run, escape) {
    const number = value => Number.isFinite(value) && value >= 0 ? value.toLocaleString() : "—";
    const selected = equipment.models.find(item => item.name === run?.model_name);
    const caption = !run ? localize("尚未运行") : ["pending", "running"].includes(run.status) ? localize("本轮用量 · 运行中，已记录数据可能尚未完整") : localize("最近一轮用量");
    const rows = [[localize("执行模型"), escape(selected?.display_name || run?.model_name || "—")],
      [localize("模型调用"), number(run?.model_call_count)], [localize("输入 Token"), number(run?.prompt_input_tokens)],
      [localize("输出 Token"), number(run?.prompt_output_tokens)], [localize("缓存命中 Token"), number(run?.prompt_cache_hit_tokens)]];
    return localize`<div class="session-usage"><p class="muted tiny">${caption}</p><dl class="inspector-run-meta">${rows.map(([key, value]) => `<div><dt>${key}</dt><dd>${value}</dd></div>`).join("")}</dl><p class="muted tiny">缓存命中包含在输入 Token 中。</p></div>`;
  }
  function render(equipment, detail, run, escape) {
    const saved = detail?.ui_state || {};
    const selected = model(equipment, saved);
    const defaultModel = equipment.models.find(item => item.name === equipment.default_model_name);
    const missing = saved.session_model_name && !selected;
    const estimate = detail?.context_usage?.estimated_tokens;
    const limit = selected?.context_window;
    const known = Number.isFinite(estimate) && estimate >= 0;
    const percent = known && limit > 0 ? Math.ceil(estimate / limit * 100) : null;
    const contextLabel = percent !== null ? localize`上下文约 ${percent}% 已用` : known ? localize`上下文约 ${estimate.toLocaleString()} Token · 窗口未知` : localize("上下文用量待加载");
    return localize`<div id="sessionControls" class="session-controls" aria-label="当前会话模型与用量"><div class="session-controls-row"><label>模型<select id="sessionModel"${detail ? "" : " disabled"} title="只影响当前会话，从下一轮生效"><option value=""${!saved.session_model_name ? " selected" : ""}>默认${defaultModel ? ` · ${escape(defaultModel.display_name)}` : localize("模型")}</option>${missing ? localize`<option selected value="${escape(saved.session_model_name)}">${escape(saved.session_model_name)}（已不可用）</option>` : ""}${equipment.models.map(item => `<option value="${escape(item.name)}"${saved.session_model_name === item.name ? " selected" : ""}>${escape(item.display_name)}</option>`).join("")}</select></label>${selected?.reasoning_efforts?.length ? localize`<label>思考<select id="sessionReasoning"><option value="">模型默认</option>${selected.reasoning_efforts.map(effort => `<option value="${escape(effort)}"${saved.session_reasoning_effort === effort ? " selected" : ""}>${labels[effort] || escape(effort)}</option>`).join("")}</select></label>` : ""}<span class="session-context${percent >= 90 ? " is-warning" : ""}" title="执行消息快照与基础提示的估算；不含本次草稿和工具定义，运行中可能滞后">${contextLabel}${percent !== null ? ` · ${estimate.toLocaleString()} / ${limit.toLocaleString()}` : ""}</span></div><details class="session-usage-details"><summary>本轮用量<span class="muted tiny">仅此会话 · 下轮生效</span></summary>${usage(equipment, run, escape)}<p class="muted tiny">上下文占用为估算，与本轮多次调用累计 Token 分开统计。</p></details></div>`;
  }
  root.FocusSessionControls = { model, request, usage, render };
  if (typeof module === "object") module.exports = root.FocusSessionControls;
})(typeof window === "object" ? window : globalThis);
