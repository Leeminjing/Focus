/* Ordinary conversation change list. Metadata refreshes independently of messages;
 * immutable snapshots and diff DOM are loaded only when a file is expanded.
 * Review prepares an editable, version-pinned request in the existing composer.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusRoundChanges = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";
  // 源文案在渲染时取当前语言；模板插值始终保留原文。
  function localize(source, ...values) {
    if (globalThis.FocusI18n?.text) return globalThis.FocusI18n.text(source, ...values);
    return typeof source === "string" ? source : source.reduce((out, part, i) => out + part + (i < values.length ? String(values[i]) : ""), "");
  }

  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const reasons = {
    get binary() { return localize("二进制文件，无法显示逐行差异"); }, get non_utf8() { return localize("非 UTF-8 文本，无法显示逐行差异"); },
    get over_1_mib() { return localize("文件超过 1 MiB，未保存完整快照"); }, get before_unobserved() { return localize("缺少写入前快照"); },
    get unreadable() { return localize("文件快照无法读取"); }, get changed_during_capture() { return localize("采集时文件发生变化"); },
    get discontinuous_versions() { return localize("两次工具写入之间文件发生了其他变化，无法可靠合并 Diff"); },
    get diff_limit() { return localize("差异超过计算上限"); }, get unavailable() { return localize("文件快照不可用"); },
  };
  const keyOf = file => `${file.first_key}/${file.last_key}`;
  const counts = file => file.added == null ? localize`<span class="round-change-unavailable">Diff 不可用</span>`
    : `<span class="round-change-added">+${file.added}</span><span class="round-change-removed">−${file.removed}</span>`;
  const detailUrl = (taskId, runId, file) => `/desktop/api/tasks/${encodeURIComponent(taskId)}/changes/${encodeURIComponent(runId)}/${encodeURIComponent(file.first_key)}/${encodeURIComponent(file.last_key)}`;

  function reviewText(data, versions, diff) {
    const text = [localize`请审查本轮代码改动，重点检查正确性、回归风险和遗漏的测试。请按严重程度给出问题，并标明文件和行号。`,
      localize`审查范围：Run ${data.run_id} 的以下不可变文件快照。不要把工作区其他未提交改动算入本轮。`];
    let budget = 60000;
    data.files.forEach((file, index) => {
      text.push(localize`\n文件：${JSON.stringify(file.path)}（${file.operation}，${file.added == null ? localize("行数未知") : `+${file.added}/-${file.removed}`}）`,
        localize`版本：${file.first_key} → ${file.last_key}${file.redacted ? localize("（已脱敏）") : ""}`);
      if (file.reason) { text.push(reasons[file.reason] || reasons.unavailable); return; }
      const version = versions[index];
      if (!version) { text.push(localize("审查正文超过读取上限；此文件未附差异，需要进一步检查。")); return; }
      const comparison = diff.buildLineDiff(version.before.text, version.after.text);
      if (comparison.limited) { text.push(reasons.diff_limit); return; }
      const rows = comparison.hunks.flatMap(hunk => comparison.rows.slice(hunk.start, hunk.end));
      let clipped = false;
      for (const row of rows) {
        const line = localize`${row.kind === "added" ? "+" : row.kind === "removed" ? "-" : " "} [旧${row.oldLine ?? "-"}/新${row.newLine ?? "-"}] ${JSON.stringify(row.text)}`;
        if (line.length > budget) { clipped = true; break; }
        text.push(line); budget -= line.length;
      }
      if (clipped) text.push(localize("差异正文已截断；此文件需要进一步检查，不能视为已完整审查。"));
    });
    return text.join("\n");
  }

  function create({ api, diff, onReview, document: doc = globalThis.document }) {
    let taskId = null, expectedRun = null, host = null, root = null, data = null;
    let generation = 0, pending = null, dirty = false, timer = null, signature = "";
    let reviewing = false;
    const snapshots = new Map();

    async function snapshot(file, frozenTask, frozenRun) {
      const key = `${frozenTask}/${frozenRun}/${keyOf(file)}`;
      if (snapshots.has(key)) return snapshots.get(key);
      const version = await api(detailUrl(frozenTask, frozenRun, file));
      // A small LRU bounds retained immutable text across repeated writes.
      snapshots.set(key, version);
      while (snapshots.size > 12) snapshots.delete(snapshots.keys().next().value);
      return version;
    }

    async function expand(row, file, frozenTask, frozenRun) {
      const body = row.querySelector("[data-change-body]");
      if (body.dataset.loaded || body.dataset.loading) return;
      if (file.reason) { body.textContent = reasons[file.reason] || reasons.unavailable; body.dataset.loaded = "1"; return; }
      body.dataset.loading = "1";
      body.textContent = localize("正在加载差异…");
      try {
        const version = await snapshot(file, frozenTask, frozenRun);
        body.innerHTML = `${file.redacted ? localize('<p class="file-diff-notice">快照已脱敏，Diff 展示脱敏后的内容。</p>') : ""}${diff.renderLineDiff(diff.buildLineDiff(version.before.text, version.after.text))}`;
        body.dataset.loaded = "1";
      } catch (error) {
        body.innerHTML = localize`<span role="status">加载差异失败：${escape(error.message)}</span> <button type="button" data-retry-diff>重试</button>`;
        body.querySelector("[data-retry-diff]").addEventListener("click", () => void expand(row, file, frozenTask, frozenRun));
      } finally { delete body.dataset.loading; }
    }

    async function review(button) {
      if (reviewing || !data?.files.length) return;
      const frozen = data, frozenTask = taskId, token = generation;
      reviewing = true; button.disabled = true; button.textContent = localize("准备审查…");
      try {
        // Serial reads keep snapshot memory and request pressure bounded.
        const versions = [];
        let retained = 0;
        for (const file of frozen.files) {
          if (token !== generation) return;
          const version = file.reason || retained >= 2 * 1024 * 1024 ? null : await snapshot(file, frozenTask, frozen.run_id);
          versions.push(version);
          if (version) retained += version.before.text.length + version.after.text.length;
        }
        if (token !== generation) return;
        if (frozen !== data) {
          root.querySelector("[data-change-status]").textContent = localize("改动已更新，请重新发起审查。");
          return;
        }
        if (onReview(reviewText(frozen, versions, diff), frozenTask) === false) {
          root.querySelector("[data-change-status]").textContent = localize("输入框暂不可用，请处理当前待办后重试。");
          return;
        }
        root.querySelector("[data-change-status]").textContent = localize("审查请求已加入输入框，可补充意见后发送。");
      } catch (error) {
        if (token === generation) root.querySelector("[data-change-status]").textContent = localize`准备审查失败：${error.message}。请重试。`;
      } finally {
        reviewing = false; button.disabled = false; button.textContent = localize("发起审查");
      }
    }

    function paint(next) {
      const nextSignature = JSON.stringify(next);
      if (signature === nextSignature) return;
      data = next;
      signature = nextSignature;
      const open = new Set([...root.querySelectorAll("details[data-change-key][open]")].map(row => row.dataset.changeKey));
      const previous = new Map([...root.querySelectorAll("details[data-change-key]")].map(row => [row.dataset.changeKey, row]));
      const panelOpen = root.querySelector(".round-changes-panel")?.open;
      const files = next.files || [];
      const running = ["pending", "running"].includes(next.status);
      const known = files.filter(file => file.added != null);
      const totals = known.reduce((sum, file) => ({ added: sum.added + file.added, removed: sum.removed + file.removed }), { added: 0, removed: 0 });
      root.innerHTML = localize`<details class="round-changes-panel"${panelOpen ? " open" : ""}><summary><strong>本轮改动</strong><span>${files.length} 个文件</span>${files.length ? `${counts(totals)}${known.length < files.length ? localize('<small>部分行数未知</small>') : ""}` : ""}${running ? localize('<small>执行中</small>') : ""}</summary><div class="round-changes-content"><div class="round-changes-toolbar"><span>本次运行的文件改动${running ? localize("，随执行更新") : ""}</span><button type="button" data-review-changes${!files.length || running ? " disabled" : ""}>发起审查</button></div><div data-change-list></div><p class="round-change-status" role="status" data-change-status>${files.length ? "" : localize("本次运行暂无已记录的文件改动。")}</p></div></details>`;
      const list = root.querySelector("[data-change-list]");
      for (const file of files) {
        const key = keyOf(file);
        let row = previous.get(key);
        if (!row) {
          row = doc.createElement("details"); row.className = "round-change-file file-version";
          row.dataset.changeKey = key;
          row.innerHTML = `<summary><span class="round-change-operation">${{ created: localize("新建"), modified: localize("修改"), deleted: localize("删除") }[file.operation] || localize("修改")}</span><span class="round-change-path" title="${escape(file.path)}">${escape(file.path)}</span>${counts(file)}</summary><div data-change-body></div>`;
          const frozenTask = taskId, frozenRun = next.run_id;
          row.addEventListener("toggle", () => { if (row.open) void expand(row, file, frozenTask, frozenRun); });
        }
        list.append(row);
        if (open.has(key)) row.open = true;
      }
      const reviewButton = root.querySelector("[data-review-changes]");
      reviewButton.addEventListener("click", () => void review(reviewButton));
    }

    async function refresh() {
      if (!host?.isConnected || !taskId) return;
      if (pending) { dirty = true; return; }
      const token = generation, frozenTask = taskId;
      const request = api(`/desktop/api/tasks/${encodeURIComponent(frozenTask)}/changes`);
      pending = request;
      try {
        const next = await request;
        if (token !== generation || !host?.isConnected) return;
        if (expectedRun && next.run_id !== expectedRun) return;
        root.querySelector("[data-refresh-error]")?.remove();
        paint(next);
      } catch (error) {
        if (token !== generation || !host?.isConnected) return;
        root.querySelector("[data-refresh-error]")?.remove();
        const notice = doc.createElement("p"); notice.dataset.refreshError = "1";
        notice.innerHTML = localize`<span role="status">本轮改动加载失败：${escape(error.message)}</span> <button type="button">重试</button>`;
        notice.querySelector("button").addEventListener("click", () => void refresh()); root.append(notice);
      } finally {
        if (pending === request) pending = null;
        if (token === generation && dirty) { dirty = false; schedule(); }
      }
    }

    function schedule() {
      clearTimeout(timer);
      timer = setTimeout(() => { timer = null; void refresh(); }, 400);
    }

    function mount(nextHost, nextTask, activeRun = null) {
      host = nextHost;
      if (!host) return;
      if (taskId !== nextTask || (activeRun && activeRun !== data?.run_id && activeRun !== expectedRun)) {
        generation++; taskId = nextTask; expectedRun = activeRun; pending = null; dirty = false;
        signature = ""; data = null; snapshots.clear();
        root = doc.createElement("section"); root.className = "round-changes"; root.setAttribute("aria-label", localize("本轮改动"));
        root.innerHTML = localize('<p class="round-change-status" role="status">正在加载本轮改动…</p>');
      } else if (!activeRun) expectedRun = null;
      host.append(root);
      void refresh();
    }

    return { mount, schedule, refresh };
  }
  return { create, reviewText };
});
