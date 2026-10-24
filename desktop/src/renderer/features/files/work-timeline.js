/* Durable work history UI. Entries are keyed by event identity, never fact identity.
 * Pagination and incremental reads share sequence cursors; unchanged articles and
 * open evidence remain mounted. Reads and details are isolated by Loop/filter generation.
 */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusWorkTimeline = api;
})(globalThis, function (root) {
  "use strict";
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const text = (source, ...values) => root.FocusI18n?.text ? root.FocusI18n.text(source, ...values) : typeof source === "string" ? source : source.reduce((s, p, i) => s + p + (i < values.length ? String(values[i]) : ""), "");
  const time = value => {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString([], { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  };
  function entryHtml(entry) {
    const icon = { user_decision: entry.intent_id ? "○" : "◈", fact: "✓", context: "⑂", run: "◇", round: "·", system: "!" }[entry.category] || "·";
    const states = { observed: text("已观察"), verifying: text("验证中"), verified: text("已验证"), contradicted: text("已被反证"), superseded: text("已替代") };
    const refs = entry.references || {};
    const stamp = entry.recorded_at;
    return `<article class="work-timeline-entry is-${escape(entry.category)}${refs.file ? " file-fact" : ""}" data-timeline-entry="${escape(entry.entry_id)}"${entry.fact_revision ? ` data-fact-revision="${escape(entry.fact_revision)}"` : ""}>
      <span class="work-timeline-icon" aria-hidden="true">${icon}</span><div class="work-timeline-heading">${refs.file ? `<button type="button" data-fact-id="${escape(entry.fact_id)}" title="${escape(refs.file.path)}">${escape(refs.file.path)}</button>` : `<strong>${escape(entry.title)}</strong>`}<time datetime="${escape(stamp)}" title="${escape(text("记录时间"))}: ${escape(stamp)}">${escape(time(stamp))}</time></div>
      ${entry.summary ? `<p>${escape(entry.summary)}</p>` : ""}<div class="work-timeline-meta">
      ${entry.round_id ? `<button type="button" data-timeline-round="${escape(entry.round_id)}" title="${escape(entry.round_id)}">R${escape(entry.round_number || "?")}</button>` : ""}
      ${(entry.context_ids || []).map(id => `<button type="button" data-timeline-context="${escape(id)}" title="${escape(id)}">${escape(refs.context_labels?.[id] || `Context ${id.slice(0, 8)}`)}</button>`).join("")}
      ${refs.state ? `<span>${escape(states[refs.state] || refs.state)} · v${escape(entry.fact_revision)}</span>` : ""}
      ${entry.intent_id ? `<button type="button" data-timeline-intent="${escape(entry.intent_id)}">${text("追踪后续")}</button>` : ""}</div>
      ${entry.fact_id ? `<details data-timeline-evidence="${escape(entry.entry_id)}"><summary>${text("查看证据")} · v${escape(entry.fact_revision)}</summary><div data-timeline-evidence-body></div></details>` : ""}
      <details class="work-timeline-source"><summary>${text("事件来源")}</summary><dl><dt>${text("记录时间")}</dt><dd>${escape(time(stamp))}</dd>${entry.occurred_at ? `<dt>${text("事实发生时间")}</dt><dd>${escape(time(entry.occurred_at))}</dd>` : ""}${["round_id", "run_id", "intent_id", "decision_id", "source_event_id"].filter(key => entry[key]).map(key => `<dt>${key}</dt><dd>${escape(entry[key])}</dd>`).join("")}</dl></details>
    </article>`;
  }
  function reconcile(host, entries) {
    const existing = new Map([...host.querySelectorAll(":scope > [data-timeline-entry]")].map(node => [node.dataset.timelineEntry, node]));
    const wanted = new Set(entries.map(entry => entry.entry_id));
    for (const [id, node] of existing) if (!wanted.has(id)) node.remove();
    host.querySelector("[data-timeline-empty]")?.remove();
    let previous = null;
    for (const entry of entries) {
      let node = existing.get(entry.entry_id);
      if (!node) {
        const template = root.document.createElement("template");
        template.innerHTML = entryHtml(entry);
        node = template.content.firstElementChild;
      }
      const expected = previous ? previous.nextElementSibling : host.firstElementChild;
      if (expected !== node) host.insertBefore(node, expected);
      previous = node;
    }
    if (!entries.length) host.innerHTML = `<p data-timeline-empty>${text("暂无工作历史，新的工作事件会显示在这里。")}</p>`;
  }
  function create({ host, api, signal }) {
    const stream = host.querySelector("[data-patrol-facts]");
    let loopId = null, authority = null, generation = 0, entries = [], after = null, before = null, loading = false, pending = false, timer = null;
    let category = "", contextId = "", roundId = "", intentId = "", selectedContext = null, source = 0, disposed = false, catchingUp = false;
    const detailsLoading = new Set();
    let fileReferences = new Set();
    const fileKey = file => JSON.stringify([file.slot_id, file.path]);
    function markFiles() {
      for (const entry of entries) {
        const file = entry.references?.file;
        if (file) stream.querySelector(`[data-timeline-entry="${entry.entry_id}"]`)?.classList.toggle("is-important", file.importance === "declared_artifact" || fileReferences.has(fileKey(file)));
      }
    }
    const options = () => ({ category, contextId, roundId, intentId, signal });
    const status = value => { const node = host.querySelector("[data-timeline-status]"); node.textContent = value; };
    function render(older = false, reset = false, announce = false) {
      const top = stream.scrollTop, oldHeight = stream.scrollHeight;
      const atEnd = oldHeight - top - stream.clientHeight < 24;
      reconcile(stream, entries);
      markFiles();
      stream.scrollTop = reset ? stream.scrollHeight : older ? top + stream.scrollHeight - oldHeight : atEnd ? stream.scrollHeight : top;
      if (announce && !older && !reset && !atEnd) host.querySelector("[data-patrol-new-facts]").hidden = false;
      host.querySelector("[data-timeline-older]").hidden = before === null;
      host.querySelector("[data-timeline-clear]").hidden = !contextId && !roundId && !intentId;
      host.querySelector("[data-timeline-scope]").textContent = intentId ? text("所选输入及后续决策") : contextId ? text("所选 Context") : roundId ? text("所选 Round") : text("跨 Round 工作历史");
    }
    function schedule() {
      if (disposed || !loopId || timer !== null) return;
      timer = setTimeout(() => { timer = null; void refresh(); }, 3000);
    }
    async function refresh(older = false) {
      if (disposed || !loopId) return;
      if (loading) { if (!older) pending = true; return; }
      if (older && before === null) return;
      loading = true;
      const owner = generation, target = loopId, initial = after === null;
      const start = older ? { before } : initial ? {} : { after };
      host.querySelector("[data-timeline-older]").disabled = true;
      if (initial) status(text("正在读取工作历史…"));
      try {
        const page = await api.timeline(target, { ...options(), ...start });
        if (owner !== generation || disposed) return;
        const newest = entries.at(-1)?.sequence || 0;
        const merged = initial ? page.entries || [] : [...entries, ...(page.entries || [])];
        entries = [...new Map(merged.map(entry => [entry.entry_id, entry])).values()].sort((a, b) => a.sequence - b.sequence);
        if (initial || older) before = page.next_before ?? null;
        if (!older) after = page.next_after;
        render(older, initial, (entries.at(-1)?.sequence || 0) > newest);
        catchingUp = page.projected_through < page.source_sequence;
        status(catchingUp ? text("正在同步新事件…") : "");
        host.querySelector("[data-timeline-retry]").hidden = true;
        if (!older && !initial && page.has_more) pending = true;
      } catch (failure) {
        if (owner === generation && !disposed && failure.name !== "AbortError") {
          status(text("工作历史读取失败，可重试。"));
          const retry = host.querySelector("[data-timeline-retry]"); retry.hidden = false; retry.dataset.older = String(older);
        }
      } finally {
        if (owner === generation) {
          loading = false;
          host.querySelector("[data-timeline-older]").disabled = false;
          if (pending) { pending = false; void refresh(); } else if (catchingUp) schedule();
        }
      }
    }
    function reset() {
      generation++; loading = false; pending = false; after = null; before = null; entries = []; detailsLoading.clear();
      if (timer !== null) clearTimeout(timer); timer = null;
      for (const item of host.querySelectorAll("[data-timeline-category]")) item.setAttribute("aria-pressed", String(item.dataset.timelineCategory === category));
      render(false, true); void refresh();
    }
    host.addEventListener("click", event => {
      const button = event.target.closest("button");
      if (!button) return;
      if (![...button.attributes].some(attr => attr.name.startsWith("data-timeline-"))) return;
      event.stopPropagation();
      if (button.hasAttribute("data-timeline-category")) {
        category = button.dataset.timelineCategory;
        for (const item of host.querySelectorAll("[data-timeline-category]")) item.setAttribute("aria-pressed", String(item === button));
        reset();
      } else if (button.hasAttribute("data-timeline-older")) void refresh(true);
      else if (button.hasAttribute("data-timeline-retry")) void refresh(button.dataset.older === "true");
      else if (button.hasAttribute("data-timeline-selected")) { contextId = selectedContext || ""; roundId = intentId = ""; reset(); }
      else if (button.hasAttribute("data-timeline-clear")) { contextId = roundId = intentId = ""; reset(); }
      else if (button.dataset.timelineContext) { contextId = button.dataset.timelineContext; roundId = intentId = ""; reset(); }
      else if (button.dataset.timelineRound) { roundId = button.dataset.timelineRound; contextId = intentId = ""; reset(); }
      else if (button.dataset.timelineIntent) { intentId = button.dataset.timelineIntent; contextId = roundId = ""; category = ""; reset(); }
      else if (button.dataset.timelineEvidenceRetry) void evidence(button.closest("details"));
    }, { signal });
    async function evidence(node) {
      const entry = entries.find(item => item.entry_id === node.dataset.timelineEvidence);
      if (!entry || detailsLoading.has(entry.entry_id) || node.dataset.loaded) return;
      const owner = generation;
      const body = node.querySelector("[data-timeline-evidence-body]");
      body.textContent = text("正在读取证据…"); detailsLoading.add(entry.entry_id);
      try {
        const detail = await api.factDetail(loopId, entry.fact_id, signal, entry.fact_revision);
        if (owner !== generation || disposed || !node.isConnected) return;
        const revision = detail.revisions?.find(item => item.revision === entry.fact_revision);
        if (!revision) throw new Error("Historical fact revision unavailable");
        const fact = { ...detail.fact, ...revision.presentation, context_id: entry.context_ids?.[0], run_id: entry.run_id,
          evidence: revision.evidence, observer: revision.observer, verifier: revision.verifier, status: revision.state, revision: revision.revision };
        body.innerHTML = root.FocusLoopFactsView.renderDetail({ ...detail, file: null, fact,
          revisions: detail.revisions.filter(item => item.revision <= entry.fact_revision), relationships: [] })
          + (detail.file ? root.FocusLoopFactsView.renderFile({ ...detail.file, fact_id: entry.fact_id, revision: entry.fact_revision }) : "");
        node.dataset.loaded = "true";
      } catch (failure) {
        if (owner === generation && !disposed && failure.name !== "AbortError") body.innerHTML = `<p>${text("历史证据读取失败。")}</p><button type="button" data-timeline-evidence-retry="${escape(entry.entry_id)}">${text("重试读取")}</button>`;
      } finally { if (owner === generation) detailsLoading.delete(entry.entry_id); }
    }
    host.addEventListener("toggle", event => { if (event.target.matches("[data-timeline-evidence]") && event.target.open) void evidence(event.target); }, { capture: true, signal });
    return Object.freeze({
      sync(id, projection) {
        const nextAuthority = projection?.loop?.state?.authority_revision;
        if (id !== loopId) { loopId = id; authority = nextAuthority ?? null; source = projection?.last_sequence || 0; category = contextId = roundId = intentId = ""; reset(); }
        else if (nextAuthority !== undefined && nextAuthority !== authority) { authority = nextAuthority; source = projection.last_sequence || 0; reset(); }
        else if ((projection?.last_sequence || 0) > source) { source = projection.last_sequence; void refresh(); }
      },
      selectContext(id) { selectedContext = id; host.querySelector("[data-timeline-selected]").disabled = !id; },
      followIntent(id) { intentId = id; category = contextId = roundId = ""; reset(); },
      setFileReferences(refs) { fileReferences = new Set(refs.map(fileKey)); markFiles(); },
      visibleFacts() {
        const bounds = stream.getBoundingClientRect();
        const visible = new Set([...stream.querySelectorAll("[data-timeline-entry]")].filter(node => {
          const rect = node.getBoundingClientRect(); return rect.bottom > bounds.top && rect.top < bounds.bottom;
        }).map(node => node.dataset.timelineEntry));
        return entries.filter(entry => entry.fact_id && visible.has(entry.entry_id)).map(entry => ({ fact_id: entry.fact_id, revision: entry.fact_revision }));
      },
      dispose() { disposed = true; generation++; if (timer !== null) clearTimeout(timer); },
    });
  }
  return Object.freeze({ create, entryHtml, reconcile });
});
