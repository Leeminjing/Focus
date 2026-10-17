/* Desktop fixed-version references use the existing Fact API and input store.
 * Picker reads are scoped to the active workspace and cancelled on close/switch.
 * The original fact/revision/result/hash identity is kept for detail and submission.
 */
(function (root) {
  "use strict";
  root.FocusDesktopPatrolReferences = { create({ host, api, store, loopId, onError }) {
    const $ = selector => host.querySelector(selector), view = root.FocusLoopFactsView;
    let reads = new AbortController(), epoch = 0, candidates = [], before = null, typed = false, rendered = "";
    function close(focus = false) {
      epoch++; reads.abort(); reads = new AbortController();
      typed = false;
      $("#version-picker").hidden = true; $("#version-detail").hidden = true;
      $("#reference-file").setAttribute("aria-expanded", "false");
      if (focus) $("#instruction").focus({ preventScroll:true });
    }
    function button(text, parent, handler) {
      const node = document.createElement("button"); node.type = "button"; node.textContent = text;
      node.addEventListener("click", () => void Promise.resolve().then(handler).catch(onError)); parent.append(node); return node;
    }
    function add(file) {
      const draft = store().get().draft, ref = view.inputFileReference(file);
      const refs = [...draft.file_refs.filter(item => item.result_key !== ref.result_key), ref];
      if (refs.length > 32) throw new Error("一次输入最多引用 32 个文件版本");
      store().edit({ file_refs:refs, ...(typed ? { content:draft.content.replace(/(^|\s)@[^@\n]*$/, "$1") } : {}) });
      close(true);
    }
    function renderCandidates() {
      const query = $("#version-query").value.toLowerCase();
      const container = $("#version-candidates"); container.replaceChildren();
      for (const file of candidates.filter(file => file.path.toLowerCase().includes(query))) {
        const item = button("", container, () => add(file));
        const icon = document.createElement("span"); icon.className = "ui-icon icon-file-text"; icon.setAttribute("aria-hidden", "true");
        const label = document.createElement("span"), name = document.createElement("b"), version = document.createElement("small");
        name.textContent = file.path.split(/[\\/]/).at(-1); name.title = file.path;
        version.textContent = `${(file.sha256 || file.result_key).slice(0,8)} · 已提交产物`;
        label.append(name,version); item.append(icon,label);
      }
      if (!container.children.length) container.textContent = query ? "没有匹配的文件版本" : "没有可引用的文件版本";
    }
    async function load(older = false) {
      const id = loopId(), owner = epoch;
      const container = $("#version-candidates");
      if (!id) { container.textContent = "没有可引用的文件版本"; return; }
      $("#older-versions").disabled = true;
      try {
        const page = await api.facts(id,{kind:"artifact",limit:100,signal:reads.signal,...(older ? {before} : {})});
        if (owner !== epoch) return;
        const files = (page.facts || []).map(view.fileReference).filter(file => file?.path && file.result_key);
        candidates = [...new Map((older ? [...candidates,...files] : files).map(file => [file.result_key,file])).values()];
        before = page.has_more ? page.next_before : null;
        $("#older-versions").hidden = before == null;
        renderCandidates();
      } catch (error) {
        if (owner !== epoch || error.name === "AbortError") return;
        container.textContent = "文件版本读取失败，草稿已保留。";
        button("重新读取",container,() => load(older));
      } finally { if (owner === epoch) $("#older-versions").disabled = false; }
    }
    async function open(fromTyping = false) {
      close(); typed = fromTyping; candidates = []; before = null;
      $("#version-query").value = fromTyping ? $("#instruction").value.match(/(?:^|\s)@([^@\n]*)$/)?.[1] || "" : "";
      $("#version-picker").hidden = false; $("#older-versions").hidden = true;
      $("#reference-file").setAttribute("aria-expanded", "true");
      $("#version-candidates").textContent = "正在读取文件版本…";
      if (!fromTyping) $("#version-query").focus({preventScroll:true});
      await load();
    }
    async function detail(file) {
      close(); typed = false;
      const owner = epoch, id = loopId(), body = $("#version-body");
      $("#version-detail").hidden = false; body.textContent = "正在读取文件版本…";
      try {
        const result = await api.factDetail(id,file.fact_id,reads.signal,file.revision);
        if (owner !== epoch) return;
        if (!result.file || result.file.result_key !== file.result_key || (file.sha256 && result.file.sha256 !== file.sha256)) throw new Error("引用版本不可用，请重新选择；原引用和要求已保留。");
        body.innerHTML = view.renderFile({ ...result.file, fact_id:file.fact_id, revision:file.revision });
        const section = body.querySelector(".file-version"), tabs = document.createElement("div");
        section.querySelector("h3").textContent = file.path.split(/[\\/]/).at(-1);
        section.querySelector("h3").title = file.path;
        section.querySelector("p").textContent = `${(file.sha256 || file.result_key).slice(0,8)} · 已提交来源${result.file.after?.redacted || result.file.before?.redacted ? " · 已脱敏" : ""}`;
        tabs.className = "version-tabs"; tabs.setAttribute("role","tablist"); tabs.setAttribute("aria-label","版本内容");
        const panels = [...section.querySelectorAll("details")];
        section.querySelectorAll(":scope > p")[1]?.classList.add("sr-only");
        const cite = section.querySelector("[data-patrol-file-add]");
        section.insertBefore(tabs,cite);
        panels.forEach((panel,index) => {
          const tab = button(panel.querySelector("summary").textContent,tabs,() => select(index));
          tab.id = `version-tab-${index}`; tab.setAttribute("role","tab"); tab.setAttribute("aria-controls",`version-tabpanel-${index}`);
          const content = document.createElement("div"); content.id = `version-tabpanel-${index}`; content.setAttribute("role","tabpanel"); content.setAttribute("aria-labelledby",tab.id); content.tabIndex = 0;
          panel.querySelector("summary").remove(); content.append(...panel.childNodes); panel.replaceWith(content);
          tab.addEventListener("keydown",event => {
            if (!["ArrowLeft","ArrowRight","Home","End"].includes(event.key)) return;
            event.preventDefault(); const next = event.key === "Home" ? 0 : event.key === "End" ? panels.length-1 : (index+(event.key === "ArrowRight" ? 1 : panels.length-1))%panels.length;
            select(next); tabs.children[next].focus();
          });
        });
        function select(index) {
          [...tabs.children].forEach((tab,i) => { tab.setAttribute("aria-selected",String(i === index)); tab.tabIndex = i === index ? 0 : -1; });
          section.querySelectorAll('[role="tabpanel"]').forEach((panel,i) => { panel.hidden = i !== index; });
        }
        select(0); section.append(cite);
        root.FocusFilePreview?.mountHistory(body, { ...result.file, fact_id: file.fact_id, revision: file.revision }, id);
        const note = document.createElement("small"); note.className = "version-note"; note.textContent = "固定版本，不随文件更新漂移"; section.append(note);
        body.querySelector("[data-patrol-file-add]")?.addEventListener("click",() => { try { add(file); } catch (error) { onError(error); } });
      } catch (error) { if (owner === epoch && error.name !== "AbortError") body.textContent = "引用版本不可用，请重新选择；原引用和要求已保留。"; }
    }
    function render(draft) {
      const refs = draft.file_refs || [], container = $("#file-refs");
      const count = (draft.attachments?.length || 0) + refs.length;
      $("#references").hidden = !count; $("#reference-count").textContent = String(count);
      const signature = JSON.stringify(refs);
      if (rendered === signature) return;
      rendered = signature; container.replaceChildren();
      for (const file of refs) {
        const row = document.createElement("div"); row.className = "file-ref";
        const icon = document.createElement("span"); icon.className = "ui-icon icon-file-text"; icon.setAttribute("aria-hidden","true"); row.append(icon);
        const name = button(file.path.split(/[\\/]/).at(-1),row,() => detail(file)); name.title = file.path;
        const version = document.createElement("small"); version.textContent = (file.sha256 || file.result_key).slice(0,8); row.append(version);
        button("移除文件引用",row,() => store().edit({file_refs:store().get().draft.file_refs.filter(item => item.result_key !== file.result_key)})).setAttribute("aria-label",`移除文件引用 ${file.path}`);
        container.append(row);
      }
    }
    $("#reference-file").addEventListener("click",() => void open().catch(onError));
    $("#close-versions").addEventListener("click",() => close(true));
    $("#close-version-detail").addEventListener("click",() => close(true));
    $("#version-query").addEventListener("input",renderCandidates);
    function navigateCandidates(event) {
      if (!["ArrowDown","ArrowUp","Home","End"].includes(event.key)) return;
      const items = [...$("#version-candidates").querySelectorAll("button")];
      if (!items.length) return;
      event.preventDefault();
      const index = items.indexOf(document.activeElement);
      const next = event.key === "Home" ? 0 : event.key === "End" ? items.length-1
        : event.key === "ArrowDown" ? (index+1)%items.length : (index < 0 ? items.length-1 : (index+items.length-1)%items.length);
      items[next].focus({preventScroll:true}); items[next].scrollIntoView({block:"nearest"});
    }
    $("#version-query").addEventListener("keydown",navigateCandidates);
    $("#version-candidates").addEventListener("keydown",navigateCandidates);
    $("#instruction").addEventListener("keydown",event => { if (!event.isComposing && !$("#version-picker").hidden) navigateCandidates(event); });
    $("#older-versions").addEventListener("click",() => void load(true));
    return {render,open,close,isOpen:() => !$("#version-picker").hidden || !$("#version-detail").hidden,
      input() {
        const match = $("#instruction").value.match(/(?:^|\s)@([^@\n]*)$/);
        if (!match || $("#reference-file").disabled) { if (typed) close(); return; }
        if (!typed || $("#version-picker").hidden) void open(true).catch(onError);
        else { $("#version-query").value = match[1]; renderCandidates(); }
      }, dispose:() => reads.abort()};
  }};
})(globalThis);
