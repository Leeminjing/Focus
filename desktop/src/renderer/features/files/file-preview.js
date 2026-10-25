/* Shared read-only preview for task files and immutable artifact snapshots.
 * All markup from files is escaped; HTML previews run in a separate native window.
 */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  root.FocusFilePreview = api;
})(globalThis, function (root) {
  "use strict";
  // 源文案在渲染时取当前语言；模板插值始终保留原文。
  function localize(source, ...values) {
    if (globalThis.FocusI18n?.text) return globalThis.FocusI18n.text(source, ...values);
    return typeof source === "string" ? source : source.reduce((out, part, i) => out + part + (i < values.length ? String(values[i]) : ""), "");
  }

  const languageViews = new WeakMap();
  function watchLanguage(node, render) {
    node.setAttribute("data-file-locale", "");
    languageViews.set(node, render);
  }
  root.document?.addEventListener?.("focus:languagechange", () => {
    root.document.querySelectorAll("[data-file-locale]").forEach(node => languageViews.get(node)?.());
  });

  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const extension = name => String(name).split(".").at(-1).toLowerCase();
  function kind(name) {
    const ext = extension(name);
    if (/^(png|jpe?g|webp|bmp|gif)$/.test(ext)) return "image";
    if (ext === "pdf") return "pdf";
    if (/^(docx?|xlsx?|pptx?|odt|ods|odp|zip|gz|7z|rar|exe|dll|wasm|mp[34]|mov|ttf|woff2?)$/.test(ext)) return "binary";
    if (/^(md|markdown)$/.test(ext)) return "markdown";
    if (/^html?$/.test(ext)) return "html";
    if (/^[ct]sv$/.test(ext)) return "csv";
    return "text";
  }
  function highlight(line, name) {
    const ext = extension(name);
    if (/^(txt|log|csv|tsv|md|markdown)$/.test(ext)) return escape(line);
    const pattern = /(\/\/.*$|\/\*.*?\*\/|#[^\n]*$|<!--.*?-->)|("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)|(\b(?:const|let|var|function|return|class|import|from|export|default|async|await|if|else|for|while|def|in|try|except|catch|throw|new|public|private|static|void|int|true|false|null|None|True|False)\b)|(\b\d+(?:\.\d+)?\b)|(<\/?[\w:-]+|\/?>)/g;
    let out = "", end = 0;
    for (const match of line.matchAll(pattern)) {
      out += escape(line.slice(end, match.index));
      const type = match[1] ? "comment" : match[2] ? "string" : match[3] ? "keyword" : match[4] ? "number" : "tag";
      out += `<span class="syntax-${type}">${escape(match[0])}</span>`;
      end = match.index + match[0].length;
    }
    return out + escape(line.slice(end));
  }
  function parseCsv(text, delimiter = ",", maxRows = 1001) {
    const rows = []; let row = [], cell = "", quoted = false;
    text = String(text).replace(/^\uFEFF/, "");
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (c === '"' && (quoted || !cell)) {
        if (quoted && text[i + 1] === '"') { cell += '"'; i++; } else quoted = !quoted;
      } else if (!quoted && c === delimiter) { row.push(cell); cell = ""; }
      else if (!quoted && (c === "\n" || c === "\r")) {
        if (c === "\r" && text[i + 1] === "\n") i++;
        row.push(cell); rows.push(row); row = []; cell = "";
        if (rows.length >= maxRows) return rows;
      } else cell += c;
    }
    if (cell || row.length || (text && !/[\r\n]$/.test(text))) { row.push(cell); rows.push(row); }
    return rows;
  }
  function toolbar(target, name) {
    const history = !!target?.loop_id;
    return localize`<div class="file-exits" role="group" aria-label="文件操作">
      <button type="button" data-file-operation="open">${history ? localize("用系统应用打开此版本") : localize("用系统应用打开")}</button>
      ${history ? localize('<button type="button" data-file-operation="current">打开当前文件</button>') : ""}
      <button type="button" data-file-operation="reveal">打开所在文件夹</button>
      <button type="button" data-file-operation="save">${history ? localize("此版本另存为") : localize("另存为")}</button>
      ${kind(name) === "html" ? localize('<button type="button" data-file-operation="html">独立预览 HTML</button>') : ""}
      <span data-file-status role="status"></span></div>`;
  }
  function bindActions(host, target, name) {
    for (const button of host.querySelectorAll("[data-file-operation]")) button.addEventListener("click", async () => {
      const status = host.querySelector("[data-file-status]");
      button.disabled = true;
      try {
        const operation = button.dataset.fileOperation;
        if (!root.focusDesktop?.fileAction) throw new Error(localize("请在 Focus 桌面应用中使用系统文件操作"));
        const result = await root.focusDesktop.fileAction({ operation: operation === "current" ? "open" : operation,
          target: operation === "current" || operation === "reveal" ? { ...target, current: true } : target, name });
        if (status) status.textContent = result === false ? localize("已取消") : localize("已完成");
      } catch (error) { if (status) status.textContent = error.message; }
      finally { button.disabled = false; }
    });
  }
  async function request(endpoint, target) {
    const response = await fetch(`/desktop/api/files/${endpoint}`, { method: "POST",
      headers: { "Content-Type": "application/json", "X-Focus-Session": root.focusDesktop?.runtime?.().session || "focus-dev-session" }, body: JSON.stringify(target) });
    if (!response.ok) {
      let detail; try { detail = (await response.json()).detail; } catch {}
      throw new Error(typeof detail === "string" ? detail : localize`读取失败 (${response.status})`);
    }
    return response;
  }
  async function mount(host, { name, target, text, onReference }) {
    const owner = {}; host.__filePreview = owner;
    host.innerHTML = localize`${toolbar(target, name)}<div class="file-preview-content" data-file-content>正在读取文件…</div>`;
    bindActions(host, target, name);
    const body = host.querySelector("[data-file-content]");
    let renderMessage = () => localize("正在读取文件…");
    watchLanguage(host, () => {
      if (host.__filePreview !== owner) return;
      const chrome = root.document.createElement("div");
      chrome.innerHTML = toolbar(target, name);
      host.querySelector(".file-exits")?.replaceWith(chrome.firstElementChild);
      bindActions(host, target, name);
      if (renderMessage) body.textContent = renderMessage();
      const previous = body.querySelector("[data-prev-page]"), next = body.querySelector("[data-next-page]");
      if (previous) previous.textContent = localize("上一页");
      if (next) next.textContent = localize("下一页");
      const image = body.querySelector(".file-preview-image");
      if (image) image.alt = kind(name) === "pdf" ? localize("PDF 页面") : localize("文件图片");
    });
    const type = kind(name);
    try {
      if (text === undefined && /\.docx?$/i.test(name)) {
        text = (await (await request("text", target)).json()).content;
      } else if (text === undefined && type === "binary") {
        renderMessage = () => localize("此格式暂无内置预览，请用系统应用打开或另存为。");
        body.textContent = renderMessage(); return;
      } else if (text === undefined) {
        const bytes = type === "pdf" ? null : await (await request("content?preview=true", target)).arrayBuffer();
        if (host.__filePreview !== owner) return;
        if (type === "image" || type === "pdf") {
          renderMessage = null;
          // PDF is rendered on the backend so no active document is embedded.
          if (type === "pdf") {
            let page = 1, pageRequest = 0;
            body.innerHTML = localize('<div class="file-preview-pages"><button type="button" data-prev-page>上一页</button><span data-page-label></span><button type="button" data-next-page>下一页</button></div><img class="file-preview-image" alt="PDF 页面">');
            const renderPage = async next => {
              const requestId = ++pageRequest;
              let data;
              try { data = await (await request(`pdf-page?page=${next}`, target)).json(); }
              catch (error) {
                if (host.__filePreview !== owner || requestId !== pageRequest) return;
                throw error;
              }
              if (host.__filePreview !== owner || requestId !== pageRequest) return;
              page = next; body.querySelector("img").src = `data:image/png;base64,${data.image}`;
              body.querySelector("[data-page-label]").textContent = `${page}/${data.page_count}`;
              body.querySelector("[data-prev-page]").disabled = page <= 1;
              body.querySelector("[data-next-page]").disabled = page >= data.page_count;
            };
            const change = next => {
              const rendering = renderPage(next), requestId = pageRequest;
              return rendering.catch(error => {
                if (host.__filePreview !== owner || requestId !== pageRequest) return;
                host.querySelector("[data-file-status]").textContent = error.message;
              });
            };
            body.querySelector("[data-prev-page]").addEventListener("click", () => change(page - 1));
            body.querySelector("[data-next-page]").addEventListener("click", () => change(page + 1));
            await renderPage(1); return;
          }
          const url = URL.createObjectURL(new Blob([bytes], { type: `image/${extension(name) === "jpg" ? "jpeg" : extension(name)}` }));
          body.innerHTML = localize('<img class="file-preview-image" alt="文件图片">');
          const img = body.querySelector("img");
          img.onload = img.onerror = () => URL.revokeObjectURL(url); img.src = url; return;
        }
        try { text = new TextDecoder("utf-8", { fatal: true }).decode(bytes); }
        catch { throw new Error(localize("文件不是 UTF-8 文本，请用系统应用打开或另存为")); }
        if (text.includes("\0")) throw new Error(localize("二进制文件无法作为文本预览，请用系统应用打开或另存为"));
      }
      if (host.__filePreview !== owner) return;
      renderMessage = null;
      renderText(body, String(text ?? ""), name, {
        range: root.FocusFileLinks?.lineRange(target.link || target),
        onReference: onReference ? value => onReference({ ...(target.link || { path: target.content_ref || name }), ...value }) : undefined,
      });
    } catch (error) {
      if (host.__filePreview === owner) {
        renderMessage = () => localize`${error.message}。可使用上方文件操作。`;
        body.textContent = renderMessage();
      }
    }
  }
  function renderText(body, text, name, { range = null, onReference } = {}) {
    let source = !!range || !["markdown", "csv"].includes(kind(name)), query = "", matches = [], selected = -1;
    let selection = range;
    const lines = text.split("\n");
    // Keep the existing 10,000-line preview/search scope, including deep links.
    const start = range?.start_line > 10000 ? Math.max(0, Math.min(range.start_line - 51, lines.length - 10000)) : 0;
    const visibleEnd = Math.min(lines.length, start + 10000);
    const searchableLines = lines.slice(start, visibleEnd).map(line => line.toLowerCase());
    let matchingLines = new Set(), sourcePre = null, lineHeight = 21.45, searchTimer = null;
    const mountedLines = new Map();
    body.innerHTML = localize`<div class="file-text-tools"><button type="button" data-source-toggle>${source ? localize("查看排版") : localize("查看源码")}</button><label><span data-file-search-label>查找</span> <input type="search" data-file-search aria-label="查找文件内容"></label><button type="button" data-search-next>下一个</button><span data-search-count role="status"></span><button type="button" data-copy-file>复制全文</button>${onReference ? localize('<button type="button" data-add-reference disabled>加入当前问题</button><span data-line-selection role="status">选择代码文本，或点击行号（Shift 连选）</span>') : ""}</div><div class="file-text-body" tabindex="0"></div>`;
    const content = body.querySelector(".file-text-body"), toggle = body.querySelector("[data-source-toggle]");
    toggle.hidden = !["markdown", "csv"].includes(kind(name));
    watchLanguage(body, () => {
      body.querySelector("[data-file-search-label]").textContent = localize("查找");
      body.querySelector("[data-file-search]").setAttribute("aria-label", localize("查找文件内容"));
      body.querySelector("[data-search-next]").textContent = localize("下一个");
      body.querySelector("[data-copy-file]").textContent = localize("复制全文");
      const reference = body.querySelector("[data-add-reference]");
      if (reference) reference.textContent = localize("加入当前问题");
      render();
    });
    function render() {
      sourcePre = null;
      mountedLines.clear();
      if (!source && kind(name) === "markdown" && root.markdownit) content.innerHTML = root.markdownit({ html: false, linkify: true }).render(text);
      else if (!source && kind(name) === "csv") {
        const rows = parseCsv(text, extension(name) === "tsv" ? "\t" : ",");
        content.innerHTML = `${rows.length > 1000 || rows.some(row => row.length > 100) ? localize("<p>仅预览前 1000 行、100 列，复制和另存为保留全文。</p>") : ""}<table class="file-csv"><tbody>${rows.slice(0, 1000).map((row, i) => `<tr>${row.slice(0, 100).map(cell => `<${i ? "td" : "th"}>${escape(cell)}</${i ? "td" : "th"}>`).join("")}</tr>`).join("")}</tbody></table>`;
      } else {
        const notice = lines.length > 10000 ? localize`<p>仅预览和查找${start ? localize`第 ${start + 1}–${visibleEnd}` : localize("前 10000")} 行，复制和另存为保留全文。</p>` : "";
        content.innerHTML = `${notice}<pre class="file-source"></pre>`;
        sourcePre = content.querySelector(".file-source");
        lineHeight = parseFloat(root.getComputedStyle?.(sourcePre).lineHeight) || 21.45;
        renderWindow();
      }
      toggle.textContent = source ? localize("查看排版") : localize("查看源码");
      updateSearchCount();
      paintSelection();
    }
    function renderWindow() {
      if (!sourcePre) return;
      // A shorter incoming row must not collapse an existing horizontal scroll position.
      const width = sourcePre.scrollWidth;
      if (width) sourcePre.style.setProperty("min-width", `${width}px`);
      const count = visibleEnd - start;
      const size = count <= 200 ? count : Math.min(200, Math.max(100, Math.ceil((content.clientHeight || 600) / lineHeight) + 80));
      const top = Math.max(0, (content.scrollTop || 0) - (sourcePre.offsetTop || 0));
      const first = Math.min(Math.max(0, count - size), Math.max(0, Math.floor(top / lineHeight) - 40));
      const last = Math.min(count, first + size);
      // Retain overlapping rows when scrolling; syntax markup is built only for new rows.
      for (const [number, node] of mountedLines) {
        if (number <= start + first || number > start + last) { node.remove(); mountedLines.delete(number); }
      }
      const markup = number => localize`<span data-line="${number}" class="file-source-line"><button type="button" class="file-line-number" data-select-line="${number}" aria-label="选择第 ${number} 行" tabindex="-1"></button>${highlight(lines[number - 1], name)}</span>`;
      let prefix = "", suffix = "";
      const existingFirst = mountedLines.size ? Number(sourcePre.firstElementChild.dataset.line) : Infinity;
      for (let number = start + first + 1; number <= start + last; number++) {
        if (mountedLines.has(number)) continue;
        if (number < existingFirst) prefix += markup(number); else suffix += markup(number);
      }
      if (prefix) sourcePre.insertAdjacentHTML("afterbegin", prefix);
      if (suffix) sourcePre.insertAdjacentHTML("beforeend", suffix);
      for (const node of sourcePre.children) {
        const number = Number(node.dataset.line);
        if (!mountedLines.has(number)) { mountedLines.set(number, node); paintLine(node, number); }
      }
      sourcePre.style.setProperty("padding-top", `${first * lineHeight}px`);
      sourcePre.style.setProperty("padding-bottom", `${(count - last) * lineHeight}px`);
    }
    function paintLine(node, number) {
      node.classList.toggle("is-match", matchingLines.has(number));
      node.classList.toggle("is-current", number === matches[selected]);
      node.classList.toggle("is-referenced", !!selection && number >= selection.start_line && number <= selection.end_line);
    }
    function updateSearchCount() {
      let count = "";
      if (query) count = selected < 0 ? localize`${matches.length} 行匹配` : localize`${selected + 1}/${matches.length} 行匹配`;
      body.querySelector("[data-search-count]").textContent = count;
    }
    function search() {
      clearTimeout(searchTimer); searchTimer = null;
      if (body.querySelector(".file-text-body") !== content) return;
      query = body.querySelector("[data-file-search]").value.toLowerCase();
      matches = [];
      if (query) {
        for (let index = 0; index < searchableLines.length; index++) {
          if (searchableLines[index].includes(query)) matches.push(start + index + 1);
        }
      }
      matchingLines = new Set(matches); selected = -1;
      if (query && !source) { source = true; render(); }
      else {
        for (const [number, node] of mountedLines) paintLine(node, number);
        updateSearchCount();
      }
    }
    function scrollToLine(number) {
      if (!sourcePre || number <= start || number > visibleEnd) return;
      content.scrollTop = Math.max(0, (sourcePre.offsetTop || 0) + (number - start - 0.5) * lineHeight - (content.clientHeight || 0) / 2);
      renderWindow();
    }
    content.addEventListener("scroll", renderWindow, { passive: true });
    function paintSelection() {
      for (const [number, node] of mountedLines) {
        node.classList.toggle("is-referenced", !!selection && number >= selection.start_line && number <= selection.end_line);
      }
      const add = body.querySelector("[data-add-reference]");
      if (add) add.disabled = !source || !selection || selection.start_line > lines.length || selection.end_line > lines.length;
      const label = body.querySelector("[data-line-selection]");
      if (label) label.textContent = selection ? localize`第 ${selection.start_line}–${selection.end_line} 行` : localize("选择代码文本，或点击行号（Shift 连选）");
    }
    let anchorLine = range?.start_line;
    content.addEventListener("click", event => {
      const button = event.target.closest?.("[data-select-line]");
      if (!button) return;
      const line = Number(button.dataset.selectLine);
      if (!event.shiftKey || !anchorLine) anchorLine = line;
      selection = { start_line: Math.min(anchorLine, line), end_line: Math.max(anchorLine, line) };
      paintSelection();
    });
    function captureSelection() {
      const selectedText = root.getSelection?.();
      if (!source || !selectedText?.rangeCount || selectedText.isCollapsed) return;
      const selectedRange = selectedText.getRangeAt(0);
      const lineFor = node => (node?.nodeType === 1 ? node : node?.parentElement)?.closest?.("[data-line]");
      const first = lineFor(selectedRange.startContainer), last = lineFor(selectedRange.endContainer);
      if (!first || !last || !content.contains(first) || !content.contains(last)) return;
      const prefix = selectedRange.cloneRange();
      prefix.setStart(last, 0);
      const end = Number(last.dataset.line) - (!prefix.toString() && last !== first ? 1 : 0);
      selection = { start_line: Number(first.dataset.line), end_line: end };
      anchorLine = selection.start_line;
      paintSelection();
    }
    content.addEventListener("mouseup", captureSelection);
    content.addEventListener("keyup", captureSelection);
    body.querySelector("[data-add-reference]")?.addEventListener("click", () => {
      if (!selection || selection.start_line > lines.length || selection.end_line > lines.length) return;
      onReference({ ...selection, text: lines.slice(selection.start_line - 1, selection.end_line).join("\n") });
    });
    toggle.addEventListener("click", () => {
      clearTimeout(searchTimer); searchTimer = null;
      source = !source; query = ""; matches = []; matchingLines.clear(); selected = -1;
      body.querySelector("[data-file-search]").value = ""; render();
    });
    body.querySelector("[data-file-search]").addEventListener("input", () => {
      clearTimeout(searchTimer); searchTimer = setTimeout(search, 150);
    });
    body.querySelector("[data-search-next]").addEventListener("click", () => {
      if (searchTimer !== null) search();
      if (!matches.length) return;
      mountedLines.get(matches[selected])?.classList.remove("is-current"); selected = (selected + 1) % matches.length;
      scrollToLine(matches[selected]);
      mountedLines.get(matches[selected])?.classList.add("is-current");
      updateSearchCount();
    });
    body.querySelector("[data-copy-file]").addEventListener("click", async event => {
      try { await root.navigator.clipboard.writeText(text); event.target.textContent = localize("已复制"); }
      catch { event.target.textContent = localize("复制失败，请选择文本复制"); }
    });
    render();
    if (range) {
      scrollToLine(range.start_line);
      if (range.start_line > lines.length || range.end_line > lines.length) body.querySelector("[data-search-count]").textContent = localize("引用行范围超出当前文件，文件可能已变化");
    }
  }
  function mountHistory(host, file, loopId) {
    if (!loopId || !file.fact_id) return;
    const preview = host.querySelector("[data-version-preview]");
    if (!preview) return;
    void mount(preview, { name: file.path, target: { loop_id: loopId, fact_id: file.fact_id, revision: file.revision },
      ...(file.after?.available ? { text: file.after.text } : {}) });
  }
  return { kind, escape, highlight, parseCsv, toolbar, bindActions, mount, mountHistory, renderText };
});
