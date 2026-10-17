/* Shared read-only preview for task files and immutable artifact snapshots.
 * All markup from files is escaped; HTML previews run in a separate native window.
 */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  root.FocusFilePreview = api;
})(globalThis, function (root) {
  "use strict";
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
    return `<div class="file-exits" role="group" aria-label="文件操作">
      <button type="button" data-file-operation="open">${history ? "用系统应用打开此版本" : "用系统应用打开"}</button>
      ${history ? '<button type="button" data-file-operation="current">打开当前文件</button>' : ""}
      <button type="button" data-file-operation="reveal">打开所在文件夹</button>
      <button type="button" data-file-operation="save">${history ? "此版本另存为" : "另存为"}</button>
      ${kind(name) === "html" ? '<button type="button" data-file-operation="html">独立预览 HTML</button>' : ""}
      <span data-file-status role="status"></span></div>`;
  }
  function bindActions(host, target, name) {
    for (const button of host.querySelectorAll("[data-file-operation]")) button.addEventListener("click", async () => {
      const status = host.querySelector("[data-file-status]");
      button.disabled = true;
      try {
        const operation = button.dataset.fileOperation;
        if (!root.focusDesktop?.fileAction) throw new Error("请在 Focus 桌面应用中使用系统文件操作");
        const result = await root.focusDesktop.fileAction({ operation: operation === "current" ? "open" : operation,
          target: operation === "current" || operation === "reveal" ? { ...target, current: true } : target, name });
        if (status) status.textContent = result === false ? "已取消" : "已完成";
      } catch (error) { if (status) status.textContent = error.message; }
      finally { button.disabled = false; }
    });
  }
  async function request(endpoint, target) {
    const response = await fetch(`/desktop/api/files/${endpoint}`, { method: "POST",
      headers: { "Content-Type": "application/json", "X-Focus-Session": root.focusDesktop?.runtime?.().session || "focus-dev-session" }, body: JSON.stringify(target) });
    if (!response.ok) {
      let detail; try { detail = (await response.json()).detail; } catch {}
      throw new Error(typeof detail === "string" ? detail : `读取失败 (${response.status})`);
    }
    return response;
  }
  async function mount(host, { name, target, text }) {
    const owner = {}; host.__filePreview = owner;
    host.innerHTML = `${toolbar(target, name)}<div class="file-preview-content" data-file-content>正在读取文件…</div>`;
    bindActions(host, target, name);
    const body = host.querySelector("[data-file-content]");
    const type = kind(name);
    try {
      if (text === undefined && /\.docx?$/i.test(name)) {
        text = (await (await request("text", target)).json()).content;
      } else if (text === undefined && type === "binary") {
        body.textContent = "此格式暂无内置预览，请用系统应用打开或另存为。"; return;
      } else if (text === undefined) {
        const bytes = type === "pdf" ? null : await (await request("content?preview=true", target)).arrayBuffer();
        if (host.__filePreview !== owner) return;
        if (type === "image" || type === "pdf") {
          // PDF is rendered on the backend so no active document is embedded.
          if (type === "pdf") {
            let page = 1;
            body.innerHTML = '<div class="file-preview-pages"><button type="button" data-prev-page>上一页</button><span data-page-label></span><button type="button" data-next-page>下一页</button></div><img class="file-preview-image" alt="PDF 页面">';
            const renderPage = async next => {
              const data = await (await request(`pdf-page?page=${next}`, target)).json();
              if (host.__filePreview !== owner) return;
              page = next; body.querySelector("img").src = `data:image/png;base64,${data.image}`;
              body.querySelector("[data-page-label]").textContent = `${page}/${data.page_count}`;
              body.querySelector("[data-prev-page]").disabled = page <= 1;
              body.querySelector("[data-next-page]").disabled = page >= data.page_count;
            };
            const change = next => renderPage(next).catch(error => { host.querySelector("[data-file-status]").textContent = error.message; });
            body.querySelector("[data-prev-page]").addEventListener("click", () => change(page - 1));
            body.querySelector("[data-next-page]").addEventListener("click", () => change(page + 1));
            await renderPage(1); return;
          }
          const url = URL.createObjectURL(new Blob([bytes], { type: `image/${extension(name) === "jpg" ? "jpeg" : extension(name)}` }));
          body.innerHTML = '<img class="file-preview-image" alt="文件图片">';
          const img = body.querySelector("img");
          img.onload = img.onerror = () => URL.revokeObjectURL(url); img.src = url; return;
        }
        try { text = new TextDecoder("utf-8", { fatal: true }).decode(bytes); }
        catch { throw new Error("文件不是 UTF-8 文本，请用系统应用打开或另存为"); }
        if (text.includes("\0")) throw new Error("二进制文件无法作为文本预览，请用系统应用打开或另存为");
      }
      if (host.__filePreview !== owner) return;
      renderText(body, String(text ?? ""), name);
    } catch (error) {
      if (host.__filePreview === owner) body.textContent = `${error.message}。可使用上方文件操作。`;
    }
  }
  function renderText(body, text, name) {
    let source = !["markdown", "csv"].includes(kind(name)), query = "", matches = [], selected = -1;
    body.innerHTML = `<div class="file-text-tools"><button type="button" data-source-toggle>${source ? "查看排版" : "查看源码"}</button><label>查找 <input type="search" data-file-search aria-label="查找文件内容"></label><button type="button" data-search-next>下一个</button><span data-search-count role="status"></span><button type="button" data-copy-file>复制全文</button></div><div class="file-text-body" tabindex="0"></div>`;
    const content = body.querySelector(".file-text-body"), toggle = body.querySelector("[data-source-toggle]");
    toggle.hidden = !["markdown", "csv"].includes(kind(name));
    function render() {
      if (!source && kind(name) === "markdown" && root.markdownit) content.innerHTML = root.markdownit({ html: false, linkify: true }).render(text);
      else if (!source && kind(name) === "csv") {
        const rows = parseCsv(text, extension(name) === "tsv" ? "\t" : ",");
        content.innerHTML = `${rows.length > 1000 || rows.some(row => row.length > 100) ? "<p>仅预览前 1000 行、100 列，复制和另存为保留全文。</p>" : ""}<table class="file-csv"><tbody>${rows.slice(0, 1000).map((row, i) => `<tr>${row.slice(0, 100).map(cell => `<${i ? "td" : "th"}>${escape(cell)}</${i ? "td" : "th"}>`).join("")}</tr>`).join("")}</tbody></table>`;
      } else {
        const lines = text.split("\n");
        content.innerHTML = `${lines.length > 10000 ? "<p>仅预览和查找前 10000 行，复制和另存为保留全文。</p>" : ""}<pre class="file-source">${lines.slice(0, 10000).map(line => `<span class="file-source-line${query && line.toLowerCase().includes(query) ? " is-match" : ""}">${highlight(line, name)}</span>`).join("")}</pre>`;
      }
      toggle.textContent = source ? "查看排版" : "查看源码";
      matches = [...content.querySelectorAll(".is-match")]; selected = -1;
      body.querySelector("[data-search-count]").textContent = query ? `${matches.length} 行匹配` : "";
    }
    toggle.addEventListener("click", () => { source = !source; query = ""; body.querySelector("[data-file-search]").value = ""; render(); });
    body.querySelector("[data-file-search]").addEventListener("input", event => { query = event.target.value.toLowerCase(); if (query) source = true; render(); });
    body.querySelector("[data-search-next]").addEventListener("click", () => {
      if (!matches.length) return;
      matches[selected]?.classList.remove("is-current"); selected = (selected + 1) % matches.length;
      matches[selected].classList.add("is-current"); matches[selected].scrollIntoView({ block: "center" });
      body.querySelector("[data-search-count]").textContent = `${selected + 1}/${matches.length} 行匹配`;
    });
    body.querySelector("[data-copy-file]").addEventListener("click", async event => {
      try { await root.navigator.clipboard.writeText(text); event.target.textContent = "已复制"; }
      catch { event.target.textContent = "复制失败，请选择文本复制"; }
    });
    render();
  }
  function mountHistory(host, file, loopId) {
    if (!loopId || !file.fact_id) return;
    const preview = host.querySelector("[data-version-preview]");
    if (!preview) return;
    void mount(preview, { name: file.path, target: { loop_id: loopId, fact_id: file.fact_id, revision: file.revision },
      ...(file.after?.available ? { text: file.after.text } : {}) });
  }
  return { kind, escape, highlight, parseCsv, toolbar, bindActions, mount, mountHistory };
});
