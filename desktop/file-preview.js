/**
 * 本文件对外提供会话页原生文件预览的宿主归属地：文件分类、标签页集合，以及把二者渲染进容器的挂载函数。
 *
 * 对外提供:
 *   classify(name) — 扩展名 → 渲染策略（image / markdown / text / pdf / binary），任意输入都有返回值
 *   createShelf() — 预览列标签页集合的状态操作（open / resolve / activate / close / has / list / isEmpty / active）
 *   renderLineNumbers(text) — 正文拆成带序号的逻辑行
 *   binaryCardModel(item) — 无法按文本呈现时的信息卡模型
 *   mount(container, shelf, view) — 把当前标签页与视图内容渲染进容器，供宿主直接调用
 *
 * 输入为文件记录（含 material_id / relative_path / size_bytes）与预览视图（分类、正文、截断标记等）。
 * 输出为渲染策略字符串、标签页集合，以及容器内的预览 DOM。
 * 具体工作流为：classify 先按扩展名决定呈现方式；createShelf 维护「打开了哪些文件、当前看哪个」；
 * 宿主按当前标签页取得预览视图后交给 mount，由 mount 分派到图片、富文本、等宽文本、文档视口或信息卡。
 * 呈现未知类型时一律落到信息卡，因此不存在无法预览的文件。
 *
 * 示例:
 *   const shelf = window.focusFilePreview.createShelf();
 *   shelf.open({ material_id: "m1", relative_path: "notes/a.md" });
 *   window.focusFilePreview.mount(container, shelf, { kind: "markdown", html: "<p>hi</p>" });
 */
(function (global) {
  "use strict";

  const IMAGE_SUFFIXES = Object.freeze(["png", "jpg", "jpeg", "webp", "bmp", "gif", "svg", "ico"]);
  const MARKDOWN_SUFFIXES = Object.freeze(["md", "markdown"]);
  const PDF_SUFFIXES = Object.freeze(["pdf"]);
  const TEXT_SUFFIXES = Object.freeze([
    "txt", "log", "json", "yaml", "yml", "toml", "ini", "cfg", "conf", "csv", "tsv",
    "xml", "html", "htm", "css", "scss", "less", "js", "mjs", "cjs", "jsx", "ts", "tsx",
    "py", "rb", "php", "sh", "bash", "ps1", "bat", "sql", "go", "rs", "java", "kt",
    "c", "h", "cpp", "hpp", "cs", "swift", "lua", "r", "pl", "vue", "svelte",
  ]);

  const KIND_BY_SUFFIX = new Map();
  for (const suffix of IMAGE_SUFFIXES) KIND_BY_SUFFIX.set(suffix, "image");
  for (const suffix of MARKDOWN_SUFFIXES) KIND_BY_SUFFIX.set(suffix, "markdown");
  for (const suffix of PDF_SUFFIXES) KIND_BY_SUFFIX.set(suffix, "pdf");
  for (const suffix of TEXT_SUFFIXES) KIND_BY_SUFFIX.set(suffix, "text");

  // 末段是否为可识别的扩展名。无点、以点结尾、末段为空一律视为无扩展名。
  function suffixOf(name) {
    const base = String(name == null ? "" : name).replace(/[?#].*$/, "").split(/[\\/]/).pop() || "";
    const dot = base.lastIndexOf(".");
    if (dot <= 0 || dot === base.length - 1) return "";
    return base.slice(dot + 1).toLowerCase();
  }

  function basenameOf(name) {
    const base = String(name == null ? "" : name).replace(/[?#].*$/, "").split(/[\\/]/).pop() || "";
    return base || String(name == null ? "" : name);
  }

  // 值域封闭：只返回五种策略之一，未登记的后缀与无后缀文件都落到 binary。
  function classify(name) {
    return KIND_BY_SUFFIX.get(suffixOf(name)) || "binary";
  }

  // 标签页标识优先取材料标识；没有材料记录的文件退化为工作区相对路径。
  function identityOf(record) {
    if (!record) return "";
    return String(record.material_id || record.relative_path || record.path || "");
  }

  // 一次记录可能来自材料列表（带 material_id），也可能只来自消息文件名（只有路径），
  // 因此匹配不能只看标识，还要让路径能命中有材料标识的同一份文件。
  function keysOf(record) {
    if (!record) return [];
    const keys = [];
    for (const value of [record.material_id, record.relative_path, record.path]) {
      if (value && !keys.includes(String(value))) keys.push(String(value));
    }
    return keys;
  }

  function matches(entry, record) {
    const keys = keysOf(record);
    if (!keys.length) return false;
    const entryKeys = keysOf(entry);
    return keys.some(key => entryKeys.includes(key));
  }

  function createShelf() {
    const entries = [];
    let activeId = null;

    // 返回已打开文件的标签页标识；未打开时返回空串。
    function resolve(record) {
      const existing = entries.find(entry => matches(entry, record));
      return existing ? identityOf(existing) : "";
    }

    function open(record) {
      const identity = identityOf(record);
      if (!identity) return { item: null, opened: false };
      const existing = entries.find(entry => matches(entry, record));
      if (existing) {
        activeId = identityOf(existing);
        return { item: existing, opened: false };
      }
      entries.push(record);
      activeId = identity;
      return { item: record, opened: true };
    }

    // 关闭一个标签页；当前标签页被关闭时接管相邻项，使列内始终有可见内容。
    function close(record) {
      const index = entries.findIndex(entry => matches(entry, record));
      if (index < 0) return false;
      entries.splice(index, 1);
      if (!entries.some(entry => identityOf(entry) === activeId)) {
        const next = entries[index] || entries[index - 1] || null;
        activeId = next ? identityOf(next) : null;
      }
      return true;
    }

    function activate(record) {
      const existing = entries.find(entry => matches(entry, record));
      if (!existing) return false;
      activeId = identityOf(existing);
      return true;
    }

    function active() {
      return entries.find(entry => identityOf(entry) === activeId) || null;
    }

    return {
      open,
      resolve,
      close,
      activate,
      active,
      has: record => !!resolve(record),
      list: () => entries.slice(),
      isEmpty: () => entries.length === 0,
      get size() { return entries.length; },
    };
  }

  function renderLineNumbers(text) {
    const body = String(text == null ? "" : text).replace(/\r\n?/g, "\n");
    const lines = body.length ? body.split("\n") : [""];
    return lines.map((line, index) => ({ number: index + 1, text: line }));
  }

  function binaryCardModel(record) {
    const item = record || {};
    const name = basenameOf(item.relative_path || item.path || "");
    const size = Number(item.size_bytes);
    return {
      name,
      path: String(item.relative_path || item.path || ""),
      suffix: suffixOf(name),
      sizeBytes: Number.isFinite(size) && size > 0 ? size : 0,
      isImage: classify(name) === "image" || Boolean(item.is_image),
      // 只有登记过的材料才能解析出工作区内的绝对路径，也才谈得上交给系统打开。
      actionable: Boolean(item.material_id),
    };
  }

  function element(document, tag, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    return node;
  }

  function appendText(document, parent, tag, className, text) {
    const node = element(document, tag, className);
    node.textContent = text;
    parent.appendChild(node);
    return node;
  }

  function renderImage(container, document, view) {
    const image = element(document, "img", "file-preview-image");
    image.src = view.url || "";
    image.alt = view.alt || "文件预览";
    if (view.materialId) {
      image.dataset.action = "zoom-image";
      image.dataset.imageUrl = view.url || "";
    }
    container.appendChild(image);
  }

  function renderHtml(container, document, view) {
    const article = element(document, "article", "file-preview-markdown");
    article.innerHTML = view.html || "";
    container.appendChild(article);
  }

  function renderText(container, document, view) {
    const pre = element(document, "pre", "file-preview-text");
    const lines = renderLineNumbers(view.text);
    for (const line of lines) {
      const row = element(document, "span", "file-preview-line");
      const gutter = appendText(document, row, "span", "file-preview-line-number", String(line.number));
      gutter.setAttribute("aria-hidden", "true");
      appendText(document, row, "span", "file-preview-line-body", line.text);
      pre.appendChild(row);
    }
    container.appendChild(pre);
  }

  function renderDocument(container, document, view) {
    const frame = element(document, "iframe", "file-preview-document");
    frame.src = view.url || "about:blank";
    frame.title = view.title || "文件预览";
    container.appendChild(frame);
  }

  // 兜底：任何无法按内容呈现的文件都得到名称、位置、大小与可执行出路，不留空白。
  function renderBinaryCard(container, document, view) {
    const model = binaryCardModel(view.item);
    const card = element(document, "section", "file-preview-binary");
    appendText(document, card, "strong", "file-preview-binary-name", model.name);
    const list = element(document, "dl", "file-preview-binary-meta");
    const rows = [
      ["类型", model.suffix ? `.${model.suffix}` : "无扩展名"],
      ["大小", model.sizeBytes ? `${model.sizeBytes} B` : "未知"],
      ["位置", model.path || "—"],
    ];
    for (const [label, value] of rows) {
      appendText(document, list, "dt", "", label);
      appendText(document, list, "dd", "", value);
    }
    card.appendChild(list);
    const actions = element(document, "div", "file-preview-binary-actions");
    if (model.actionable) {
      const open = appendText(document, actions, "button", "text-button", "在系统中打开");
      open.type = "button";
      open.dataset.action = "open-preview-in-system";
      open.dataset.filePath = model.path;
      const download = appendText(document, actions, "button", "text-button", "下载");
      download.type = "button";
      download.dataset.action = "download-preview-file";
      download.dataset.filePath = model.path;
    } else {
      appendText(document, actions, "span", "muted tiny", "该文件尚未登记为材料，无法定位工作区路径");
    }
    card.appendChild(actions);
    container.appendChild(card);
  }

  const RENDERERS = {
    image: renderImage,
    markdown: renderHtml,
    text: renderText,
    pdf: renderDocument,
    binary: renderBinaryCard,
  };

  // 按当前标签页与视图内容重绘容器；容器无标签页时留空，由宿主负责隐藏整列。
  function mount(container, shelf, view) {
    if (!container) return;
    const document = container.ownerDocument || global.document;
    if (!document) return;
    container.replaceChildren();
    const current = view && view.kind ? view : null;
    if (!current) return;
    const render = RENDERERS[current.kind] || renderBinaryCard;
    render(container, document, current);
  }

  global.focusFilePreview = {
    classify,
    createShelf,
    renderLineNumbers,
    binaryCardModel,
    mount,
  };
})(window);
