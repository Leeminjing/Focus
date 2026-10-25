/*
 * 本文件对外提供 Agent Loop 可追溯事实抽屉、renderRows、reconcileRows、时间流 renderTimeline/reconcileTimeline、共用详情及验证状态/业务结果筛选选项。
 * 输入为物化事实、当前 Context 和筛选；输出为事实表或时间流 HTML，或按 fact_id 原位更新的 DOM；两种呈现共用状态和证据标签。
 * 具体工作流为只呈现领域 revision 并排除历史 tool 条目；未变行不移出 tbody、不改写签名，按实际顺序原位更新状态；稳定 fact_id 来源按钮进入真实证据检查。
 * 示例：`FocusLoopFactsView.reconcileRows(tbody, facts)`。
 * 文件事件使用紧凑条目和显式来源高亮；renderFile 按执行位置与不可变快照展示预览和可靠 Diff，所有正文转义后输出。
 * buildLineDiff 生成有计算上限的最短逐行编辑路径、双侧行号及多变更块；详情折叠未改内容并限制渲染量。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusLoopFactsView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";
  // 源文案在渲染时取当前语言；模板插值始终保留原文。
  function localize(source, ...values) {
    if (globalThis.FocusI18n?.text) return globalThis.FocusI18n.text(source, ...values);
    return typeof source === "string" ? source : source.reduce((out, part, i) => out + part + (i < values.length ? String(values[i]) : ""), "");
  }

  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);

  const statusOptions = () => [["", localize("全部事实状态")], ["observed", localize("已观察")], ["verifying", localize("验证中")], ["verified", localize("已验证")], ["contradicted", localize("已被反证")], ["superseded", localize("已替代")]];
  const outcomeOptions = () => [["", localize("全部业务结果")], ["success", localize("成功")], ["failed", localize("失败")], ["unknown", localize("未知")]];
  const factStates = () => Object.fromEntries(statusOptions());
  const outcomeStates = () => Object.fromEntries(outcomeOptions());
  function evidenceLabel(item) {
    return item.run_id || item.evidence?.[0]?.run_id || item.evidence?.run_id || item.evidence?.tool_name || item.evidence?.message_id || "evidence";
  }

  function timeLabel(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function rowHtml(item) {
    return `<tr data-fact-id="${escape(item.fact_id)}" data-fact-revision="${escape(item.revision || 1)}" class="is-${escape(item.status)}"><td>${escape(timeLabel(item.occurred_at))}</td><td><strong>${escape(item.context_id)}</strong></td><td><span class="fact-kind">${escape(item.kind)}</span></td><td><strong>${escape(item.title)}</strong><span>${escape(item.summary)}</span></td><td><span class="fact-status is-${escape(item.status)}">${escape(factStates()[item.status] || item.status)}</span>${item.outcome_status ? `<small>${escape(outcomeStates()[item.outcome_status] || item.outcome_status)}</small>` : ""}</td><td><button class="text-button" data-action="inspect-loop-fact" data-fact-id="${escape(item.fact_id)}">${escape(evidenceLabel(item))} ↗</button></td></tr>`;
  }

  function timelineHtml(item) {
    const file = fileReference(item);
    if (file) {
      const importance = item.file_importance || file.importance;
      const label = importance === "user_reference" ? localize("用户引用 · ") : importance === "declared_artifact" ? localize("关键产出 · ") : "";
      return localize`<article data-fact-id="${escape(item.fact_id)}" data-fact-revision="${escape(item.revision || 1)}" class="file-fact${label ? " is-important" : ""} is-${escape(item.status)}"><time>${escape(timeLabel(item.occurred_at))}</time><button data-fact-id="${escape(item.fact_id)}" data-action="inspect-loop-fact" title="${escape(file.path)}">${escape(file.path)}</button><small>${label}${escape({ created: localize("新建"), modified: localize("修改"), deleted: localize("删除") }[file.operation] || localize("文件"))} · ${escape((file.sha256 || file.result_key).slice(0, 8))} · 查看版本</small></article>`;
    }
    return `<article data-fact-id="${escape(item.fact_id)}" data-fact-revision="${escape(item.revision || 1)}" class="is-${escape(item.status)}"><time>${escape(timeLabel(item.occurred_at))}</time><button data-fact-id="${escape(item.fact_id)}" data-action="inspect-loop-fact">${escape(item.title || item.kind)}</button><p>${escape(item.summary)}</p><small>${escape(factStates()[item.status] || item.status || localize("待确认"))}${item.outcome_status ? localize` · 结果${escape(outcomeStates()[item.outcome_status] || item.outcome_status)}` : ""}</small></article>`;
  }
  function renderTimeline(items) {
    return items.filter(item => item.kind !== "tool").map(timelineHtml).join("") || localize('<p data-fact-empty>暂无可验证事实</p>');
  }
  const reconcileTimeline = (host, items) => reconcileRows(host, items, "timeline");

  function renderRows(items) {
    return items.filter(item => item.kind !== "tool").map(rowHtml).join("") || localize('<tr data-fact-empty><td colspan="6" class="history-boundary">暂无可验证事实</td></tr>');
  }

  function reconcileRows(tbody, items, presentation = "table") {
    const itemHtml = presentation === "timeline" ? timelineHtml : rowHtml;
    items = items.filter(item => item.kind !== "tool");
    if (!tbody || typeof document !== "object") return false;
    const existing = new Map([...tbody.querySelectorAll(":scope > [data-fact-id]")].map(row => [row.dataset.factId, row]));
    const focusId = tbody.contains(document.activeElement) ? document.activeElement.dataset.factId : null;
    let previous = null;
    for (const item of items) {
      const signature = JSON.stringify(item);
      let row = existing.get(item.fact_id);
      if (!row) {
        const template = document.createElement("template");
        template.innerHTML = itemHtml(item);
        row = template.content.firstElementChild;
      } else {
        existing.delete(item.fact_id);
        if (row.dataset.factSignature !== signature) {
          const template = document.createElement("template");
          template.innerHTML = itemHtml(item);
          const next = template.content.firstElementChild;
          row.className = next.className;
          row.dataset.factRevision = next.dataset.factRevision;
          row.innerHTML = next.innerHTML;
        }
      }
      if (row.dataset.factSignature !== signature) row.dataset.factSignature = signature;
      const reference = previous ? previous.nextSibling : tbody.firstChild;
      if (row !== reference) tbody.insertBefore(row, reference);
      previous = row;
    }
    existing.forEach(row => row.remove());
    if (items.length) tbody.querySelector("[data-fact-empty]")?.remove();
    if (!items.length && !tbody.querySelector("[data-fact-empty]")) {
      const template = document.createElement("template");
      template.innerHTML = presentation === "timeline" ? renderTimeline(items) : renderRows(items);
      tbody.append(template.content.firstElementChild);
    }
    const target = focusId ? tbody.querySelector(`button[data-fact-id="${CSS.escape(focusId)}"]`) : null;
    if (target && document.activeElement !== target) target.focus({ preventScroll: true });
    return true;
  }

  function renderDetail(detail) {
    if (detail.file) return renderFile(detail.file);
    const fact = detail.fact || {};
    const metrics = fact.metrics || {};
    return localize`<section class="fact-inspection"><h3>${escape(fact.title || fact.kind)}</h3><p>${escape(fact.summary)}</p><p>事实状态：${escape(factStates()[fact.status] || fact.status)} · 业务结果：${escape(outcomeStates()[fact.outcome_status] || fact.outcome_status || localize("未知"))}</p>${fact.kind === "test" ? `<p>${metrics.count_status === "exact" ? `${escape(metrics.passed ?? 0)} passed · ${escape(metrics.failed ?? 0)} failed · ${escape(metrics.skipped ?? 0)} skipped` : localize("测试数量尚未明确，不计为成功")}</p>` : ""}<h3>事实修订</h3><ul>${(detail.revisions || []).map(row => localize`<li>版本 ${row.revision} · ${escape(factStates()[row.state] || row.state)}<p>${escape(row.reason || row.presentation?.summary)}</p></li>`).join("") || localize("<li>暂无修订历史</li>")}</ul><details><summary>证据与精确来源</summary><pre>${escape(JSON.stringify({ fact_id: fact.fact_id, context_id: fact.context_id, run_id: fact.run_id, evidence: fact.evidence, relationships: detail.relationships }, null, 2))}</pre></details>${fact.evidence ? "" : localize("<p>证据正文未在当前权限下返回。</p>")}</section>`;
  }

  function fileReference(fact) {
    const file = fact.file || fact.presentation?.file || (Array.isArray(fact.evidence) ? fact.evidence.find(item => item.kind === "artifact" && item.metadata?.file)?.metadata.file : null);
    return file ? { ...file, fact_id: fact.fact_id, revision: fact.revision } : null;
  }
  function inputFileReference(file) {
    return Object.fromEntries(["fact_id", "revision", "result_key", "slot_id", "path", "sha256"].map(key => [key, file[key] ?? null]));
  }
  function markFileReferences(items, references) {
    const selected = new Set(references.map(item => JSON.stringify([item.slot_id, item.path])));
    return items.map(item => { const file = fileReference(item); return file && selected.has(JSON.stringify([file.slot_id, file.path])) ? { ...item, file_importance: "user_reference" } : item; });
  }

  // Myers shortest edit path. Bound both search work and retained backtracking
  // state; exceeding either budget must never turn unchanged lines into edits.
  function lineEdits(left, right) {
    if (!left.length) return right.map(text => ({ kind: "added", text }));
    if (!right.length) return left.map(text => ({ kind: "removed", text }));
    const leftLines = new Set(left);
    if (!right.some(line => leftLines.has(line))) return [
      ...left.map(text => ({ kind: "removed", text })),
      ...right.map(text => ({ kind: "added", text })),
    ];
    let frontier = new Map([[1, 0]]), work = 0, entries = 0;
    const trace = [];
    for (let distance = 0; distance <= left.length + right.length; distance++) {
      const next = new Map();
      for (let diagonal = -distance; diagonal <= distance; diagonal += 2) {
        if (++work > 2000000 || ++entries > 250000) return null;
        const down = diagonal === -distance || (diagonal !== distance && frontier.get(diagonal - 1) < frontier.get(diagonal + 1));
        let x = down ? frontier.get(diagonal + 1) : frontier.get(diagonal - 1) + 1;
        let y = x - diagonal;
        while (x < left.length && y < right.length && left[x] === right[y]) {
          x++; y++;
          if (++work > 2000000) return null;
        }
        next.set(diagonal, x);
        if (x >= left.length && y >= right.length) {
          const edits = [];
          for (let depth = distance; depth > 0; depth--) {
            const previous = trace[depth - 1], k = x - y;
            const fromDown = k === -depth || (k !== depth && previous.get(k - 1) < previous.get(k + 1));
            const previousK = fromDown ? k + 1 : k - 1;
            const previousX = previous.get(previousK), previousY = previousX - previousK;
            while (x > previousX && y > previousY) {
              edits.push({ kind: "equal", text: left[--x] }); y--;
            }
            if (fromDown) edits.push({ kind: "added", text: right[--y] });
            else edits.push({ kind: "removed", text: left[--x] });
          }
          while (x > 0 && y > 0) { edits.push({ kind: "equal", text: left[--x] }); y--; }
          return edits.reverse();
        }
      }
      trace.push(next);
      frontier = next;
    }
    return null;
  }

  function buildLineDiff(before, after) {
    // Keep line terminators in comparisons, including a missing final newline.
    // An empty file has zero lines; a trailing newline is not an extra blank line.
    if (before.length + after.length > 2 * 1024 * 1024) return { limited: true };
    const left = before.match(/[^\n]*\n|[^\n]+$/g) || [];
    const right = after.match(/[^\n]*\n|[^\n]+$/g) || [];
    if (left.length + right.length > 100000) return { limited: true };
    let prefix = 0, suffix = 0;
    while (prefix < Math.min(left.length, right.length) && left[prefix] === right[prefix]) prefix++;
    while (suffix < Math.min(left.length, right.length) - prefix && left[left.length - suffix - 1] === right[right.length - suffix - 1]) suffix++;
    const edits = lineEdits(left.slice(prefix, left.length - suffix), right.slice(prefix, right.length - suffix));
    if (!edits) return { limited: true };
    const rows = [
      ...left.slice(0, prefix).map(text => ({ kind: "equal", text })),
      ...edits,
      ...(suffix ? left.slice(-suffix).map(text => ({ kind: "equal", text })) : []),
    ];
    let oldLine = 1, newLine = 1, added = 0, removed = 0;
    for (const row of rows) {
      row.oldBefore = oldLine - 1; row.newBefore = newLine - 1;
      row.oldLine = row.kind === "added" ? null : oldLine++;
      row.newLine = row.kind === "removed" ? null : newLine++;
      if (row.kind === "added") added++;
      if (row.kind === "removed") removed++;
    }
    const hunks = [];
    rows.forEach((row, index) => {
      if (row.kind === "equal") return;
      const start = Math.max(0, index - 3), end = Math.min(rows.length, index + 4);
      const previous = hunks.at(-1);
      if (previous && start <= previous.end) previous.end = end;
      else hunks.push({ start, end });
    });
    for (const hunk of hunks) {
      const lines = rows.slice(hunk.start, hunk.end);
      hunk.oldCount = lines.filter(row => row.oldLine !== null).length;
      hunk.newCount = lines.filter(row => row.newLine !== null).length;
      hunk.oldStart = rows[hunk.start].oldBefore + (hunk.oldCount ? 1 : 0);
      hunk.newStart = rows[hunk.start].newBefore + (hunk.newCount ? 1 : 0);
    }
    return { limited: false, rows, hunks, added, removed };
  }

  function renderLineDiff(comparison) {
    if (comparison.limited) return localize('<p class="file-diff-notice">文件差异过大，已停止逐行比较；请查看前后完整版本。</p>');
    const { rows, hunks, added, removed } = comparison;
    if (!hunks.length) return localize('<p class="file-diff-summary">无逐行变更 · +0 / −0</p>');
    // Separate budgets keep collapsed context from crowding out actual edits.
    const budgets = { visible: { rows: 1000, characters: 120000 }, folded: { rows: 1000, characters: 60000 } };
    function renderRows(start, end, folded = false) {
      const html = [];
      const budget = folded ? budgets.folded : budgets.visible;
      let index = start;
      for (; index < end && budget.characters > 0 && budget.rows > 0; index++) {
        const row = rows[index];
        budget.rows--;
        const text = row.text.replace(/\n$/, "").replace(/\r/g, "␍");
        const shown = text.slice(0, Math.min(2000, budget.characters));
        budget.characters -= shown.length + 1;
        const marker = row.kind === "added" ? "+" : row.kind === "removed" ? "−" : " ";
        html.push(localize`<div class="file-diff-row is-${row.kind}" data-old-line="${row.oldLine ?? ""}" data-new-line="${row.newLine ?? ""}"><code class="file-diff-number" aria-label="旧行 ${row.oldLine ?? localize("无")}">${row.oldLine ?? ""}</code><code class="file-diff-number" aria-label="新行 ${row.newLine ?? localize("无")}">${row.newLine ?? ""}</code><span class="${row.kind}">${marker}${escape(shown)}${shown.length < text.length ? localize('<small class="file-diff-notice"> … 此行显示已截断</small>') : ""}</span></div>${row.text.endsWith("\n") ? "" : localize('<div class="file-diff-note">\\ 文件末尾无换行</div>')}`);
      }
      if (index < end) html.push(localize`<p class="file-diff-notice">其余 ${end - index} 行未渲染，请查看前后完整版本。</p>`);
      return html.join("");
    }
    function foldedRows(start, end) {
      return end > start ? localize`<details class="file-diff-fold"><summary>展开 ${end - start} 行未修改内容</summary><div class="file-diff-lines">${renderRows(start, end, true)}</div></details>` : "";
    }
    let cursor = 0;
    const blocks = [];
    for (const [index, hunk] of hunks.entries()) {
      if (budgets.visible.rows <= 0 || budgets.visible.characters <= 0) {
        blocks.push(localize`<p class="file-diff-notice">其余 ${hunks.length - index} 个变更块未渲染，请查看前后完整版本。统计包含全部变更。</p>`);
        cursor = rows.length;
        break;
      }
      const content = renderRows(hunk.start, hunk.end);
      blocks.push(localize`${foldedRows(cursor, hunk.start)}<section class="file-diff-hunk" aria-label="变更块 ${index + 1}"><header>@@ -${hunk.oldStart},${hunk.oldCount} +${hunk.newStart},${hunk.newCount} @@</header><div class="file-diff-lines">${content}</div></section>`);
      cursor = hunk.end;
    }
    blocks.push(foldedRows(cursor, rows.length));
    return localize`<p class="file-diff-summary">${hunks.length} 个变更块 · 新增 ${added} 行 · 删除 ${removed} 行（+${added} / −${removed}）</p><div class="file-diff" aria-label="逐行差异"><div class="file-diff-columns">旧行　 新行　 内容</div>${blocks.join("")}</div>`;
  }

  function renderFile(file) {
    const reasons = { before_unobserved: localize("写入前版本未被观察，无法生成可靠 Diff"), unreadable: localize("无法读取"), outside_slot: localize("超出工作区"), over_1_mib: localize("超过 1 MiB，未保存正文"), binary: localize("二进制文件"), non_utf8: localize("非 UTF-8 文本") };
    const snapshot = side => side?.available ? `<pre>${escape(side.text)}</pre>` : `<p>${escape(reasons[side?.reason] || localize("历史快照不可用"))}</p>`;
    const before = file.before, after = file.after;
    const reliable = before?.available && after?.available;
    const comparison = reliable ? buildLineDiff(before.text, after.text) : null;
    const summary = comparison && !comparison.limited ? ` · +${comparison.added} / −${comparison.removed}` : "";
    return localize`<section class="file-version"><h3>${escape(file.path)}</h3><p>版本 ${(file.sha256 || file.result_key).slice(0, 12)}${before?.redacted || after?.redacted ? localize(" · 已脱敏") : ""}</p><p>执行位置 ${escape(file.slot_id)}${file.context_id ? localize` · 工作线 ${escape(file.context_id)}` : ""}</p><button type="button" data-patrol-file-add="${escape(JSON.stringify(inputFileReference(file)))}">＠ 引用此版本</button><details open><summary>此版本内容</summary><div data-version-preview>${snapshot(after)}</div></details><details><summary>Diff${reliable ? summary : localize(" · 不可用")}</summary>${reliable ? renderLineDiff(comparison) : snapshot(before?.available ? after : before)}</details><details><summary>写入前内容</summary>${snapshot(before)}</details></section>`;
  }
  return Object.freeze({ renderRows, reconcileRows, renderTimeline, reconcileTimeline, renderDetail, renderFile, buildLineDiff, renderLineDiff, fileReference, inputFileReference, markFileReferences, get statusOptions() { return statusOptions(); }, get outcomeOptions() { return outcomeOptions(); } });
});
