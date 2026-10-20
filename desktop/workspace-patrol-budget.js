/* 工作区预算入口：从服务端读取预设，组合 Live 授权、结算/预留消费和 Run 状态。
 * 首次选择跟随输入草稿冻结；追加携带授权版本，复用根授权事务，保持累计用量。
 * create 持有的只有展示状态；dispose 释放时钟和监听，过期读响应不写入其它工作区。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.FocusWorkspacePatrolBudget = api;
})(globalThis, function () {
  "use strict";
  // 源文案在渲染时取当前语言；模板插值始终保留原文。
  function localize(source, ...values) {
    if (globalThis.FocusI18n?.text) return globalThis.FocusI18n.text(source, ...values);
    return typeof source === "string" ? source : source.reduce((out, part, i) => out + part + (i < values.length ? String(values[i]) : ""), "");
  }

  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const number = value => Number(value).toLocaleString("zh-CN");
  const fields = () => [["rounds", localize("轮次")], ["duration_seconds", localize("时长")], ["model_calls", localize("模型调用")], ["input_tokens", localize("输入 token")], ["output_tokens", localize("输出 token")], ["retries", localize("重试")]];
  const display = (key, value) => value == null ? localize("未设额外上限") : key === "duration_seconds" ? localize`${number(Math.ceil(value / 60))} 分钟` : number(value);
  function markup() {
    return localize('<section class="patrol-budget" data-patrol-budget aria-label="本次自动执行预算"><label><span data-budget-label>任务预算</span> <select data-budget-preset aria-label="任务预算预设" disabled><option value="normal">普通任务</option><option value="long">长任务</option></select></label><div data-budget-summary role="status">正在读取预算…</div><details><summary>预算明细与追加</summary><div data-budget-details></div><p>这是执行上限，可能提前完成；检查预算时停止派发新工作，已在途调用可能稍后结算。时长从任务创建起计算，包含暂停和等待。token 为用量单位，费用取决于模型价格。实际并发还受运行池约束。</p><button type="button" data-budget-increase hidden>增加本次预算</button><button type="button" data-budget-retry hidden>重新读取预算</button><p data-budget-message role="status"></p></details></section>');
  }
  function summary(projection, now = Date.now()) {
    const loop = projection?.loop?.state;
    if (!loop?.grant) return null;
    const accounting = projection.accounting?.state || {};
    const usage = { ...(loop.usage || {}), ...(accounting.usage || {}) };
    const consumption = (accounting.accounting || loop.accounting)?.consumption;
    const start = Date.parse(loop.created_at);
    if (Number.isFinite(start)) usage.duration_seconds = Math.max(0, Math.floor(((Date.parse(loop.completed_at) || now) - start) / 1000));
    const budgets = loop.grant.budgets || {};
    const rows = fields().map(([key, label]) => {
      const occupied = consumption?.occupied?.[key] ?? usage[key];
      const actual = consumption?.actual?.[key] ?? occupied;
      const reserved = consumption?.unreported_reservations?.[key] ?? 0;
      const limit = budgets[`max_${key}`];
      return { key, label, actual, reserved, occupied, limit, remaining: limit == null || occupied == null ? null : Math.max(0, limit - occupied) };
    });
    const runs = Object.values(projection.runs || {}).map(entity => entity.state);
    const waits = Object.values(projection.wait_requests || {}).filter(entity => ["open", "resolving"].includes(entity.state.status));
    const terminal = ["completed", "stopped", "failed"].includes(loop.status);
    return { loop, rows, running: runs.filter(run => run.status === "running").length,
      queued: runs.filter(run => ["pending", "queued"].includes(run.status)).length,
      concurrency: budgets.max_concurrent_runs, terminal,
      canIncrease: !terminal && loop.grant.status === "active" && (loop.status !== "waiting_user" || waits.some(entity => entity.state.kind === "budget_action")),
      budgetWait: waits.some(entity => entity.state.kind === "budget_action"),
      needsAttention: loop.status === "waiting_user" || waits.length > 0,
      near: rows.some(row => row.limit != null && row.occupied != null && row.occupied >= row.limit * 0.8) };
  }
  function presetDetails(preset) {
    return fields().map(([key, label]) => `${label} ${display(key, preset.budgets[`max_${key}`])}`).join(" · ") + localize` · 授权并发 ${preset.budgets.max_concurrent_runs}`;
  }
  function create({ host, api, getDraft = () => ({}), editDraft = () => {}, onError = () => {}, canChooseInitial = true }) {
    const select = host.querySelector("[data-budget-preset]"), output = host.querySelector("[data-budget-summary]");
    const details = host.querySelector("[data-budget-details]"), increase = host.querySelector("[data-budget-increase]");
    const message = host.querySelector("[data-budget-message]"), retry = host.querySelector("[data-budget-retry]");
    const events = new AbortController();
    globalThis.document?.addEventListener?.("focus:languagechange", () => {
      const section = host.querySelector("[data-patrol-budget]");
      section?.setAttribute("aria-label", localize("本次自动执行预算"));
      for (const [value, label] of [["normal", localize("普通任务")], ["long", localize("长任务")]]) {
        const option = select.querySelector(`option[value="${value}"]`);
        if (option) option.textContent = label;
      }
      const caption = section?.querySelector("details > summary");
      if (caption) caption.textContent = localize("预算明细与追加");
      const help = section?.querySelector("details > p");
      if (help) help.textContent = localize("这是执行上限，可能提前完成；检查预算时停止派发新工作，已在途调用可能稍后结算。时长从任务创建起计算，包含暂停和等待。token 为用量单位，费用取决于模型价格。实际并发还受运行池约束。");
      retry.textContent = localize("重新读取预算");
      render();
    }, { signal: events.signal });
    let catalog = null, projection = null, disposed = false, pending = false, loadedLoop = false;
    function render() {
      if (disposed || !catalog) return;
      const draftPreset = getDraft().budget_preset;
      if (!loadedLoop && draftPreset && catalog.presets[draftPreset]) select.value = draftPreset;
      const selected = catalog.presets[select.value] || catalog.presets[catalog.default];
      const model = summary(projection);
      host.querySelector("[data-budget-label]").textContent = loadedLoop ? localize("追加份额") : localize("任务预算");
      select.setAttribute("aria-label", loadedLoop ? localize("追加预算份额") : localize("任务预算预设"));
      select.disabled = pending || (!loadedLoop && !canChooseInitial);
      if (!model) {
        output.textContent = loadedLoop ? localize("正在同步当前授权与用量…") : !canChooseInitial ? localize("首次预算可在桌面输入面板选择") : localize`首次启动采用${localize(selected.label)} · 最多 ${display("duration_seconds", selected.budgets.max_duration_seconds)} · 最多 ${selected.budgets.max_concurrent_runs} 项并行`;
        details.textContent = presetDetails(selected);
        increase.hidden = true;
        return;
      }
      const duration = model.rows.find(row => row.key === "duration_seconds");
      const notice = model.terminal ? localize("本次工作已结束") : model.budgetWait ? localize("预算已用尽，需要你追加预算或停止") : model.needsAttention ? localize("需要你处理待办") : model.near ? localize("预算即将用尽") : localize("自动执行预算");
      output.textContent = localize`${notice} · 已用 ${duration.occupied == null ? localize("时长待同步") : display(duration.key, duration.occupied)} · 剩余 ${duration.limit == null ? localize("时间未设限") : duration.remaining == null ? localize("时间待同步") : display(duration.key, duration.remaining)} · 正在并行 ${model.running} / 授权 ${model.concurrency ?? localize("待同步")}${model.queued ? localize` · 排队 ${model.queued}` : ""}`;
      details.innerHTML = localize`<ul>${model.rows.map(row => localize`<li>${escape(row.label)}：已结算 ${row.actual == null ? localize("待同步") : display(row.key, row.actual)}${row.reserved ? localize`，预留／待确认 ${display(row.key, row.reserved)}` : ""} / 上限 ${display(row.key, row.limit)}；剩余 ${row.limit == null ? localize("未设额外上限") : row.remaining == null ? localize("待同步") : display(row.key, row.remaining)}</li>`).join("")}</ul><p>追加一份${escape(localize(selected.label))}：${escape(presetDetails(selected))}。追加基于当前上限或已占用量中较大值；未设限字段保持未设限，轮次累计最多 1,000。${model.loop.status === "paused" ? localize("追加后保持暂停。") : localize("追加后按新预算继续执行。")}</p>`;
      increase.hidden = !model.canIncrease;
      increase.disabled = pending;
      increase.textContent = pending ? localize("正在增加预算…") : localize`增加一份${localize(selected.label)}预算`;
    }
    async function load() {
      try {
        const value = await api.budgetPresets();
        if (disposed) return;
        catalog = value;
        select.value = getDraft().budget_preset || catalog.default;
        retry.hidden = true; message.textContent = ""; render();
      } catch (error) {
        if (disposed) return;
        output.textContent = localize("预算暂时不可用，请重试读取；未选择预设时首次发送采用普通任务");
        retry.hidden = false; message.textContent = error.message;
      }
    }
    select.addEventListener("change", () => { if (!loadedLoop) editDraft({ budget_preset: select.value }); render(); }, { signal: events.signal });
    async function refresh(id) {
      const latest = await api.get(id);
      if (disposed || projection?.loop_id !== id || latest.authority_revision < projection.loop?.state?.authority_revision) return;
      projection = { ...projection, loop: { ...projection.loop, state: { ...projection.loop.state, ...latest } },
        ...(latest.accounting ? { accounting: { state: { accounting: latest.accounting, usage: latest.usage } } } : {}) };
      retry.hidden = true;
    }
    retry.addEventListener("click", () => {
      if (!catalog || !projection?.loop_id) { void load(); return; }
      void refresh(projection.loop_id).then(render).catch(error => { if (!disposed) message.textContent = error.message; });
    }, { signal: events.signal });
    increase.addEventListener("click", async () => {
      const model = summary(projection);
      if (pending || !model?.canIncrease) return;
      const current = projection, id = current.loop_id;
      pending = true; message.textContent = ""; render();
      try {
        await api.mutateGrant(id, { command: "increase_budget", preset: select.value, expected_authority_revision: model.loop.authority_revision });
        await refresh(id);
        if (disposed || projection?.loop_id !== id) return;
        message.textContent = localize("本次预算已增加，累计用量保持不变。");
      } catch (error) { if (!disposed && projection?.loop_id === id) { message.textContent = error.message; retry.hidden = false; onError(error); } }
      finally { pending = false; render(); }
    }, { signal: events.signal });
    void load();
    const timer = setInterval(render, 15000);
    return Object.freeze({ update(value) { projection = value; loadedLoop = Boolean(value?.loop); render(); }, syncDraft: render,
      dispose() { disposed = true; clearInterval(timer); events.abort(); } });
  }
  return Object.freeze({ markup, summary, presetDetails, create });
});
