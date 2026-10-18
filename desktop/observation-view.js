/* 本文件对外提供冻结 Observation 的摘要与分页检查 HTML。
 * 输入为同一 Observation 白名单响应和已读取 section；输出为人类可读依据、可选紧凑工作台摘要、独立前序与折叠精确身份。
 * 具体工作流为按精确 V 与逐输入 Oi/Ui 呈现完整因果清单和分页，分别显示接收边界、事实覆盖、历史权限与当前逐轮执行授权；实时资格单独标示，不填充历史。
 * 同一检查入口按所选版本读取 Patrol 阶段、等待目标及 Curator 失败信息，审计记录可独立刷新。
 * 示例：FocusObservationView.render(summary, { sources: page })。
 */
(function (root) {
  "use strict";
  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const short = value => escape(value ? String(value).slice(0, 10) : "不可用");
  const sourceNames = { user_revision: "用户要求", run_outcome: "执行结果", test: "测试事实", artifact: "产物", workspace: "工作区结果" };
  const states = { completed: "已验证", in_progress: "进行中", not_started: "待开展", blocked: "阻塞", not_applicable: "被替代", unknown: "待判断", conflicted: "存在冲突", success: "已确认", failed: "失败" };
  const decisions = { current: "当前认知 · 尚未提交", superseded: "已被新观察版本抢占", rejected: "候选被拒绝 · 尚未提交", committed: "权威决策已提交" };
  const modes = { "read-only": "只读执行", "workspace-write": "工作区可写", "danger-full-access": "完全访问" };
  const patrolPhases = { created: "已创建", freezing_observation: "冻结观察", observing: "观察中", dispatching_curators: "派发 Curator", collecting_curators: "收集 Curator", proposing: "生成提案", authorizing: "授权中", delivering: "交付中", awaiting_evidence: "等待证据", publishing: "发布中", completed: "已完成", failed: "失败", interrupted: "已中断", superseded: "已失效" };
  const curatorStates = { queued: "排队中", reading: "读取中", analyzing: "分析中", running: "运行中", proposed: "已提交提案", consumed: "已采用", failed: "失败", cancelled: "已取消", superseded: "已失效" };
  function patrolHistory(summary, page) {
    const title = `<h3>Patrol 阶段历史 · V${escape(summary.generation ?? "—")}</h3>`;
    if (!page) return `<section>${title}<button data-patrol-observation-section="patrol_history">读取此版本的阶段与 Curator</button></section>`;
    if (!page.patrol_session) return `<section>${title}<p>此 Observation 版本尚无 Patrol 会话记录。</p><button data-patrol-observation-section="patrol_history">刷新此版本的审计记录</button></section>`;
    const session = page.patrol_session;
    const entries = page.items.map(item => item.history_kind === "phase"
      ? `<li><strong>${escape(patrolPhases[item.from_phase] || item.from_phase)} → ${escape(patrolPhases[item.to_phase] || item.to_phase)}</strong><p>${time(item.occurred_at)} · ${escape(item.summary)}</p>${item.wait_reason ? `<p>等待原因：${escape(item.wait_reason)}</p>` : ""}${item.wait_targets?.length ? `<p>等待目标：${item.wait_targets.map(target => `${escape(target.entity_type)} · ${escape(target.entity_id)}`).join("；")}</p>` : ""}${identities(item)}</li>`
      : `<li><strong>Curator · ${escape(curatorStates[item.state] || item.state)}</strong><p>Worker ${escape(item.worker_request_id)}</p>${item.result_summary ? `<p>${escape(item.result_summary)}</p>` : ""}${item.failure ? `<p>失败原因：${escape(item.failure)}</p>` : ""}${identities(item)}</li>`).join("");
    return `<section>${title}<p>${escape(patrolPhases[session.phase] || session.phase)} · ${escape(session.status)} · 已读取 ${page.items.length} / ${page.total} 条记录</p><ul class="observation-source-list">${entries || "<li>此会话尚无阶段与 Curator 记录</li>"}</ul><button data-patrol-observation-section="patrol_history">${page.has_more ? "加载后续审计记录" : "刷新此版本的审计记录"}</button>${identities(session)}</section>`;
  }
  function time(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? escape(value) : escape(date.toLocaleString([], { hour12: false }));
  }
  function identities(item) {
    const entries = Object.entries(item).filter(([key]) => key !== "evidence");
    return `<details class="observation-audit"><summary>精确身份与版本</summary><dl>${entries.map(([key, value]) => `<div><dt>${escape(key)}</dt><dd>${escape(typeof value === "object" ? JSON.stringify(value) : value)}</dd></div>`).join("")}</dl></details>`;
  }
  function itemView(item, section) {
    if (section === "snapshot" && item.snapshot_section) return itemView(item, item.snapshot_section);
    if (section === "versions") return `<li><button data-patrol-observation-version="${escape(item.observation_id)}">查看 V${escape(item.generation)}</button><p>${time(item.frozen_at)}</p>${identities(item)}</li>`;
    if (section === "causal_chain") return `<li><strong>O${escape(item.generation)} → U${escape(item.generation)}</strong><p>受理于 ${time(item.accepted_at)} · 服务器边界 ${escape(item.accepted_sequence ?? "未知")}</p><pre>${escape(item.input?.content || "")}</pre><button data-patrol-observation-version="${escape(item.observation_id)}">检查这条输入的原始快照</button>${identities(item)}</li>`;
    if (section === "snapshot") {
      const labels = { rounds: "输入前 Round", runs: "输入前 Run", directives: "指令", workers: "认知工作", dispatches: "调度", publications: "发布", adoptions: "采纳", fact: "当时的 Fact", visible_fact: "客户端显示的 Fact", visible_run: "客户端运行片段", permission_ceiling: "当时的权限上限", projection_failure: "事实投影缺口" };
      const mode = item.access_mode || item.equipment?.access_mode;
      return `<li><strong>${labels[item.kind] || escape(item.kind)}${item.number ? ` ${escape(item.number)}` : ""}</strong><p>${escape(item.status || item.state || "")}${mode ? ` · ${modes[mode] || escape(mode)}` : ""}${item.revision ? ` · revision ${escape(item.revision)}` : ""}</p>${item.kind === "visible_run" ? `<p>独立 Run 游标 ${escape(item.sequence ?? "未知")} · ${item.truncated ? "已截断" : "有界片段"} · ${item.gap ? "存在缺口" : "未报告缺口"}</p>` : ""}${item.evidence ? `<pre>${escape(item.evidence.text ?? JSON.stringify(item.evidence, null, 2))}</pre>` : ""}${identities(item)}</li>`;
    }
    if (section === "previous_progress") return `<li><strong>${escape(item.description || item.item_id)}</strong><p>${states[item.state] || escape(item.state)} · ${item.support === "supported" ? "有证据" : item.support === "asserted" ? "已报告" : "待验证"}</p>${item.blockers?.length ? `<p>${escape(item.blockers.join("；"))}</p>` : ""}${identities(item)}</li>`;
    if (section === "lineage") return `<li><strong>${item.kind === "edge" ? `${short(item.source_context_id)} → ${short(item.target_context_id)}` : `Context ${short(item.context_id)} · R${escape(item.generation)}`}</strong>${identities(item)}</li>`;
    const evidence = item.evidence;
    const metrics = evidence?.metrics;
    return `<li><strong>${sourceNames[item.kind] || escape(item.kind)} · ${short(item.source_id)}</strong>${evidence ? `<p>${escape(evidence.summary || evidence.title || states[evidence.status] || evidence.status || "保留原来源状态")}</p>${metrics ? `<p>${metrics.count_status === "exact" ? `${escape(metrics.passed ?? 0)} passed · ${escape(metrics.failed ?? 0)} failed · ${escape(metrics.skipped ?? 0)} skipped` : "测试数量尚未明确"}</p>` : ""}` : "<p>证据正文未在当前权限下返回。</p>"}${identities(item)}</li>`;
  }
  function coverage(summary) {
    const fields = [["冻结来源覆盖", summary.sources?.complete], ["前序历史覆盖", summary.previous_progress?.history_complete], ["来源关系覆盖", summary.lineage?.complete]];
    return `<p class="observation-coverage">${fields.map(([label, value]) => `${label}：${value === true ? "完整" : value === false ? "不完整" : "未确认"}`).join(" · ")}</p>`;
  }
  function card(summary, { compact = false } = {}) {
    if (!summary) return "本轮尚未冻结";
    if (compact) return `<h3>OBSERVATION · Round ${escape(summary.round_number ?? "—")} · V${escape(summary.generation ?? "—")}</h3><p>${decisions[summary.decision_state] || "冻结观察"} · ${summary.causal_chain_count || 0} 条输入因果关系</p><p>本轮冻结 ${summary.sources.total} 个任务来源与 ${summary.lineage.node_count} 个 Revision。</p>`;
    return `<h3>Round ${escape(summary.round_number ?? "—")} 看到了什么</h3><p>本轮冻结 ${summary.sources.total} 个任务来源与 ${summary.lineage.node_count} 个 Revision。</p><small>基于前序 ${short(summary.previous_progress.progress_id)} · ${time(summary.frozen_at)}</small>${coverage(summary)}`;
  }
  function render(summary, sections = {}) {
    if (!summary) return "尚未取得冻结输入";
    const titles = { versions: "截至此版本的 Observation 历史", causal_chain: "逐输入 Oi → Ui 因果链", snapshot: "这条输入之前的工作局面", previous_progress: "本轮前序 Task Progress", sources: "本轮冻结来源", lineage: "精确的 Context frontier" };
    return `<div class="observation-inspection"><div class="observation-meta"><span>不可变快照 · Round ${escape(summary.round_number ?? "—")}</span><span>冻结于 ${time(summary.frozen_at)}</span></div><h3 class="observation-question">这一轮，基于什么作决定？</h3><p>本轮观察与上一轮进度一起进入决策。新的表达仍可随时提交，但不会改写已经冻结的输入。</p>${boundary(summary)}${coverage(summary)}${summary.availability === "legacy" ? '<p role="status">旧记录没有完整冻结读面；不使用当前 Progress 回填。</p>' : ""}<div class="observation-identities"><section><h3>上一轮 Task Progress</h3><strong>${short(summary.previous_progress.progress_id)}</strong><p>${summary.previous_progress.item_count} 个冻结条目</p>${summary.previous_progress.progress_id ? `<button data-patrol-observation-progress="${escape(summary.previous_progress.progress_id)}">检查本轮前序版本 ↗</button>` : ""}</section><section><h3>本轮 Observation</h3><strong>${short(summary.observation_id)}</strong><p>${summary.sources.total} 个任务来源 · ${summary.lineage.node_count} 个 Revision</p></section></div><p class="observation-frontier">精确 Context frontier · ${summary.lineage.node_count} 个冻结 Revision · ${summary.lineage.edge_count ?? 0} 条来源关系</p>${!summary.evidence_visible ? '<p>当前权限不包含证据正文，仅显示允许的身份与状态。</p>' : ""}${Object.entries(titles).map(([key, title]) => {
      const page = sections[key];
      return `<section><h3>${title}</h3>${page ? `<p>当前冻结清单已读取 ${page.items.length} / ${page.total} 项${page.has_more ? " · 仍有后续" : ""}</p><ul class="observation-source-list">${page.items.map(item => itemView(item, key)).join("") || "<li>此冻结范围没有条目</li>"}</ul>${page.has_more ? `<button data-patrol-observation-section="${key}">加载下一页</button>` : ""}` : `<button data-patrol-observation-section="${key}">读取冻结条目</button>`}</section>`;
    }).join("")}${patrolHistory(summary, sections.patrol_history)}${identities({ observation_id: summary.observation_id, observation_hash: summary.observation_hash, inputs_hash: summary.inputs_hash, previous_progress_id: summary.previous_progress.progress_id, previous_progress_hash: summary.previous_progress.content_hash, goal_revision: summary.goal_revision, authority_revision: summary.authority_revision })}<p>Observation 是本轮冻结输入，LoopFact 是持续更新的事实记录。</p></div>`;
  }
  function boundary(summary) {
    const value = summary.boundary || {};
    return `<p>V${escape(summary.generation ?? "—")} · ${decisions[summary.decision_state] || "冻结观察"}</p><p>服务器受理前边界 ${escape(value.server_sequence ?? "未知")} · 客户端 Live 位置 ${escape(value.live_sequence ?? "未知")} · ${value.client_context_known ? "已保存客户端可见线索" : "客户端可见范围未知"}</p><p>当时 ${escape(value.round_count ?? 0)} 个活动 Round · ${escape(value.fact_count ?? 0)} 个 Fact revision · Fact 投影位置 ${escape(value.projection_cursor ?? "未知")} · ${escape(value.projection_failures ?? 0)} 个投影缺口</p>`;
  }
  function permissionValidity(projection, run, round) {
    const binding = run.round_authorization, loop = projection.loop?.state;
    const directive = projection.directives?.[run.directive_id]?.state;
    if (!binding) return null;
    if (["settled", "error", "superseded"].includes(round.status) || run.settlement_status === "settled" || ["success", "error", "interrupted", "cancelled"].includes(run.status)
        || loop && loop.status !== "running" || loop?.authority_revision && loop.authority_revision !== binding.grant_revision
        || ["cancelled", "cancel_requested", "stale", "failed"].includes(directive?.state)
        || loop?.grant?.expires_at && Date.parse(loop.grant.expires_at) <= Date.now()) return false;
    return run.permission_state?.valid ?? null;
  }
  function rounds(projection) {
    const items = Object.values(projection.rounds || {}).sort((a, b) => b.state.number - a.state.number);
    const current = items[0]?.state;
    return `${strategyAdoptions(current?.strategy_adoptions)}<details class="observation-rounds"><summary>轮次与执行权限 · ${items.length} 轮</summary>${items.map(entity => {
      const round = entity.state;
      const runs = Object.values(projection.runs || {}).filter(run => run.state.round_id === entity.entity_id);
      return `<section><h4>Round ${escape(round.number)} · V${escape(round.observation_generation ?? "—")}</h4><p>${escape(round.status)} · ${round.authority_committed_at ? "权威决策已提交" : "尚无权威提交"}</p>${strategyAdoptions(round.strategy_adoptions)}${round.observation_id ? `<button data-patrol-observation-version="${escape(round.observation_id)}">检查该轮当前观察</button>` : ""}${round.barrier && Object.keys(round.barrier).length ? `<details><summary>汇合等待依据</summary><pre>${escape(JSON.stringify(round.barrier, null, 2))}</pre></details>` : ""}<ul>${runs.map(entity => {
        const run = entity.state, grant = run.round_authorization, valid = permissionValidity(projection, run, round);
        return `<li>Context ${short(run.context_id)} · Run ${short(entity.entity_id)}<p>${grant ? `${modes[grant.access_mode] || escape(grant.access_mode)} · ${grant.source === "migration" ? "历史迁移授权" : "Patrol 本轮授权"} · ${valid === true ? "有效" : valid === false ? "已失效" : "资格待核验"}${run.permission_state?.reason ? `（${escape(run.permission_state.reason)}）` : ""}` : "本轮授权未记录"}</p><p>${escape(run.status)} · ${run.settlement_status === "settled" ? "已结算" : "等待执行清理与结算"}</p></li>`;
      }).join("") || "<li>尚无执行 Run</li>"}</ul></section>`;
    }).join("")}</details>`;
  }
  function strategyAdoptions(records) {
    return (records || []).map(record => `<section class="strategy-adoption"><strong>已采用「${escape(record.title)}」</strong><p>${escape(record.reason)}</p><details><summary>策略版本与影响的安排</summary><p>${escape(record.package?.id || record.memory_id)} · v${escape(record.package?.version || "历史版本")}</p><p>动作 ${(record.action_indexes || []).map(index => index + 1).join("、")} · ${escape(record.content_hash)}</p></details></section>`).join("");
  }
  root.FocusObservationView = Object.freeze({ card, render, rounds, strategyAdoptions });
  if (typeof module === "object" && module.exports) module.exports = root.FocusObservationView;
})(globalThis);
