/* Shared, on-demand file drawer for the robot and workspace. The current Loop
 * defines “this work”; all artifact pages participate in search and history.
 * Group by slot + relative path, never by basename or fact verification revision. */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusArtifactDrawer = api;
})(globalThis, function (root) {
  "use strict";
  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const name = file => file.path.split(/[\\/]/).at(-1);
  function groupFiles(facts, query = "") {
    const groups = new Map();
    for (const fact of facts) {
      const file = fact.file || fact.presentation?.file;
      if (!file?.path || !file.slot_id || !file.result_key) continue;
      const key = JSON.stringify([file.slot_id, file.path]);
      if (!groups.has(key)) groups.set(key, new Map());
      const versions = groups.get(key), previous = versions.get(file.result_key);
      if (!previous || (previous.revision || 0) < (fact.revision || 0)) versions.set(file.result_key, { ...fact, file });
    }
    return [...groups.values()].map(versions => [...versions.values()].sort((a, b) =>
      (Date.parse(b.occurred_at) || 0) - (Date.parse(a.occurred_at) || 0) || b.fact_id.localeCompare(a.fact_id)))
      .filter(versions => versions[0].file.operation !== "deleted" && name(versions[0].file).toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))
      .sort((a, b) => (Date.parse(b[0].occurred_at) || 0) - (Date.parse(a[0].occurred_at) || 0));
  }
  function create({ host, api, getLoopId, onSource, bridge = root.focusDesktop?.files }) {
    const drawer = root.document.createElement("aside");
    drawer.className = "artifact-drawer"; drawer.hidden = true;
    drawer.setAttribute("aria-label", "文件 / 产物");
    drawer.innerHTML = '<header><h2>文件 / 产物</h2><button type="button" data-files-close aria-label="关闭文件抽屉">×</button></header><p class="artifact-scope">本次工作新建、修改的文件</p><div class="artifact-toolbar"><input type="search" data-files-search aria-label="按文件名搜索" placeholder="搜索文件名…"><button type="button" data-files-refresh>刷新</button></div><p data-files-status role="status"></p><div data-files-list></div><div data-files-preview hidden></div>';
    host.prepend(drawer);
    const $ = selector => drawer.querySelector(selector);
    const lifetime = new AbortController();
    let reads = new AbortController(), epoch = 0, previewVersion = 0, liveKey = "", facts = [], selected = [], returnFocus = null;
    function status(message) { $("[data-files-status]").textContent = message; }
    const date = value => { const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? "修改时间未知" : parsed.toLocaleString(); };
    function render() {
      selected = [];
      const button = (fact, label, historical = false) => {
        const index = selected.push(fact) - 1;
        return `<button type="button" data-files-view="${index}">${escape(label)}</button>${!historical && bridge ? `<div class="artifact-exits"><button type="button" data-files-native="open" data-index="${index}">打开</button><button type="button" data-files-native="reveal" data-index="${index}">所在文件夹</button><button type="button" data-files-native="saveAs" data-index="${index}">另存</button></div>` : ""}<button type="button" data-files-source="${index}"${fact.context_id ? "" : " disabled"}>来源 Context</button>`;
      };
      const groups = groupFiles(facts, $("[data-files-search]").value);
      $("[data-files-list]").innerHTML = groups.map(versions => {
        const latest = versions[0];
        return `<article class="artifact-row"><strong>${escape(name(latest.file))}</strong><span class="artifact-path">${escape(latest.file.path)}</span><time>${latest.file.operation === "created" ? "新建" : "修改"} · ${escape(date(latest.occurred_at))}</time><div class="artifact-actions">${button(latest, "查看")}</div><details><summary>历史版本（${versions.length - 1}）</summary>${versions.slice(1).map(fact => `<div class="artifact-history"><time>${escape(date(fact.occurred_at))}</time>${button(fact, "查看历史快照", true)}</div>`).join("") || "暂无更早版本"}</details></article>`;
      }).join("") || `<p>${$("[data-files-search]").value ? "没有匹配的文件" : "本次工作尚无文件产物。文件生成后可在这里查看和另存。"}</p>`;
    }
    async function refresh() {
      if (drawer.hidden) return;
      reads.abort(); reads = new AbortController();
      const signal = reads.signal, owner = ++epoch, id = getLoopId(); previewVersion++;
      facts = []; render(); $("[data-files-preview]").hidden = true;
      status("正在读取文件…");
      if (!id) { status("发送任务并生成文件后，产物会显示在这里。"); return; }
      try {
        const all = []; let before;
        do {
          const page = await api.facts(id, { kind: "artifact", limit: 200, before, signal });
          if (owner !== epoch || id !== getLoopId()) return;
          all.push(...(page.facts || []));
          if (!page.has_more) break;
          if (page.next_before == null || page.next_before === before) throw new Error("文件分页不可用，请刷新重试");
          before = page.next_before;
        } while (true);
        facts = all; render(); status(`${groupFiles(facts).length} 个文件 · 当前磁盘文件可打开或另存，历史版本可查看快照`);
      } catch (error) { if (owner === epoch && error.name !== "AbortError") status(`文件读取失败：${error.message}。可点击刷新重试。`); }
    }
    function close(focus = true) {
      epoch++; previewVersion++; reads.abort(); drawer.hidden = true;
      if (focus && returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
    }
    drawer.addEventListener("input", event => { if (event.target === $("[data-files-search]")) render(); }, { signal: lifetime.signal });
    drawer.addEventListener("keydown", event => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); }
    }, { signal: lifetime.signal });
    drawer.addEventListener("click", async event => {
      const target = event.target.closest("button");
      if (!target) return;
      event.stopPropagation();
      if (target.hasAttribute("data-files-close")) { close(); return; }
      if (target.hasAttribute("data-files-refresh")) { await refresh(); return; }
      const index = target.dataset.filesView ?? target.dataset.filesSource ?? target.dataset.index;
      const fact = selected[Number(index)];
      if (!fact) return;
      const owner = epoch, id = getLoopId();
      try {
        target.disabled = true;
        if (target.hasAttribute("data-files-source")) await onSource(fact.context_id);
        else if (target.dataset.filesNative) {
          const result = await bridge[target.dataset.filesNative]({ loopId: id, factId: fact.fact_id, revision: fact.revision });
          if (owner === epoch && !result?.canceled) status(target.dataset.filesNative === "saveAs" ? "已另存文件" : "已打开");
        } else {
          const version = ++previewVersion;
          const detail = await api.factDetail(id, fact.fact_id, reads.signal, fact.revision);
          if (owner !== epoch || version !== previewVersion || id !== getLoopId()) return;
          if (!detail.file || detail.file.result_key !== fact.file.result_key || (fact.file.sha256 && detail.file.sha256 !== fact.file.sha256)) throw new Error("此版本快照不可用");
          const file = { ...detail.file, fact_id: fact.fact_id, revision: fact.revision };
          $("[data-files-preview]").innerHTML = `<h3>历史版本 · 固定快照</h3>${root.FocusLoopFactsView.renderFile(file)}`;
          $("[data-files-preview]").querySelector("[data-patrol-file-add]")?.remove();
          root.FocusFilePreview?.mountHistory($("[data-files-preview]"), file, id);
          $("[data-files-preview]").hidden = false;
          $("[data-files-preview]").scrollIntoView({ block: "nearest" });
        }
      } catch (error) { if (owner === epoch && error.name !== "AbortError") status(error.message); }
      finally { if (target.isConnected) target.disabled = false; }
    }, { signal: lifetime.signal });
    return { open() { returnFocus = root.document.activeElement; drawer.hidden = false; $("[data-files-search]").value = ""; $("[data-files-search]").focus(); return refresh(); },
      close, refresh, isOpen: () => !drawer.hidden,
      update(items) {
        const key = JSON.stringify(items.filter(item => item.file || item.presentation?.file).map(item => [item.fact_id, item.revision]));
        if (key === liveKey) return;
        liveKey = key; if (!drawer.hidden) void refresh();
      },
      reset() { facts = []; liveKey = ""; close(false); },
      dispose() { close(false); lifetime.abort(); drawer.remove(); } };
  }
  return Object.freeze({ groupFiles, create });
});
