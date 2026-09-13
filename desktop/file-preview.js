/**
 * 本文件对外提供会话页原生文件预览的宿主归属地：文件分类、文本解码、标签页集合、地址构造，
 * 以及把预览视图渲染进容器的挂载函数。
 *
 * 对外提供:
 *   classify(name) — 扩展名 → 渲染策略（image / markdown / text / pdf / binary），任意输入都有返回值
 *   decodeText(bytes) — 字节 → { text, encoding }，按 UTF-8 → GB18030 逐级解码
 *   createShelf() — 预览列标签页集合的状态操作（open / resolve / activate / close / has / list / isEmpty / active）
 *   createObjectUrls() — 由字节构造可呈现地址并保证每个地址都被回收
 *   renderLineNumbers(text) — 正文拆成带序号的逻辑行
 *   binaryCardModel(item) — 无法按文本呈现时的信息卡模型
 *   mount(container, shelf, view) — 把当前标签页与视图内容渲染进容器，供宿主直接调用
 *
 * 输入为文件记录（含 material_id / relative_path / path / size_bytes）、已读出的字节与预览视图
 * （分类、正文、截断标记、编码等）。
 * 输出为渲染策略字符串、解码结果、标签页集合、可呈现地址，以及容器内的预览 DOM。
 * 具体工作流为：classify 先按扩展名决定呈现方式；宿主取得字节后交给 decodeText 或 createObjectUrls；
 * createShelf 维护「打开了哪些文件、当前看哪个」；宿主按当前标签页取得预览视图后交给 mount，
 * 由 mount 先渲染统一头部（名称 / 大小 / 截断 / 编码）再分派到图片、富文本、等宽文本、文档视口或信息卡。
 * 呈现未知类型时一律落到信息卡，因此不存在无法预览的文件。
 *
 * 示例:
 *   const shelf = window.focusFilePreview.createShelf();
 *   shelf.open({ material_id: "m1", relative_path: "notes/a.md" });
 *   const { text, encoding } = window.focusFilePreview.decodeText(bytes);
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

  // 与后端文本预览的编码链同序：先 UTF-8（含 BOM），失败再退 GB18030（GBK/GB2312 的超集）。
  // 这份链只服务「按路径读取」的字节：那条链上的文本必须由渲染器解码，
  // 因为主进程所用的 Node TextDecoder 不支持 gb18030（实测），而浏览器支持。
  const DECODE_ENCODINGS = Object.freeze(["utf-8", "gb18030"]);

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

  // 后缀 → 构造 Blob 地址用的媒体类型。按路径预览的字节不经 HTTP，因此没有响应头可依，
  // 必须由这里给出类型；给出空串时浏览器虽会嗅探，但显式声明更确定。
  const MEDIA_TYPE_BY_SUFFIX = Object.freeze({
    png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", webp: "image/webp",
    bmp: "image/bmp", gif: "image/gif", svg: "image/svg+xml", ico: "image/x-icon",
    pdf: "application/pdf",
  });

  function mediaTypeForName(name) {
    return MEDIA_TYPE_BY_SUFFIX[suffixOf(name)] || "";
  }

  // 字节 → 文本。逐级尝试解码链，全部失败时以替换字符兜底而不是抛错——
  // 预览应当给出「尽力而为的正文」，而不是让整列空掉。
  function decodeText(bytes) {
    const data = bytes || new Uint8Array(0);
    for (const encoding of DECODE_ENCODINGS) {
      try {
        return { text: new TextDecoder(encoding, { fatal: true }).decode(data), encoding };
      } catch (error) {
        continue;
      }
    }
    return { text: new TextDecoder("utf-8").decode(data), encoding: "utf-8/replace" };
  }

  // 由字节构造可呈现地址，并保证每个地址在替换或关闭时被回收，避免持续占用内存。
  function createObjectUrls(urlApi) {
    const urls = new Map();
    function urlFor(key, bytes, mediaType) {
      const previous = urls.get(key);
      if (previous) urlApi.revokeObjectURL(previous);
      const url = urlApi.createObjectURL(new Blob([bytes], { type: mediaType || "" }));
      urls.set(key, url);
      return url;
    }
    function revoke(key) {
      const url = urls.get(key);
      if (!url) return false;
      urlApi.revokeObjectURL(url);
      urls.delete(key);
      return true;
    }
    return { urlFor, revoke, keys: () => [...urls.keys()] };
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
      // 交给系统打开需要磁盘上的绝对路径；按路径预览的文件与材料记录都可能提供它
      openPath: String(item.path || ""),
      suffix: suffixOf(name),
      sizeBytes: Number.isFinite(size) && size > 0 ? size : 0,
      isImage: classify(name) === "image" || Boolean(item.is_image),
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

  function formatBytes(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value) || value <= 0) return "";
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / 1024 / 1024).toFixed(1)} MB`;
  }

  // 统一头部：五种策略都需要「我在看哪个文件」，因此身份与状态标识只在这里渲染一次。
  function renderHeader(container, document, view) {
    const model = binaryCardModel(view.item);
    const header = element(document, "header", "file-preview-head");
    appendText(document, header, "strong", "file-preview-name", model.name || "文件预览");
    const size = formatBytes(model.sizeBytes);
    if (size) appendText(document, header, "span", "file-preview-size", size);
    const meta = view.meta || {};
    if (meta.truncated) appendText(document, header, "span", "file-preview-flag is-warning", "内容已截断");
    if (meta.encoding && meta.encoding !== "utf-8") {
      appendText(document, header, "span", "file-preview-flag", meta.encoding);
    }
    container.appendChild(header);
  }

  function renderImage(container, document, view) {
    const image = element(document, "img", "file-preview-image");
    image.src = view.url || "";
    image.alt = view.alt || "文件预览";
    if (view.materialId) {
      image.dataset.action = "zoom-image";
      image.dataset.imageUrl = view.url || "";
    }
    // 地址失效或字节损坏时给出可见说明，而不是留一个碎图占位
    image.onerror = () => {
      image.remove();
      appendText(container, document, "p", "file-preview-empty", "该图片无法显示，可能已被移动或内容有损。");
    };
    container.appendChild(image);
  }

  function renderHtml(container, document, view) {
    const article = element(document, "article", "file-preview-markdown");
    article.innerHTML = view.html || "";
    container.appendChild(article);
  }

  function renderText(container, document, view) {
    if (!view.text) {
      appendText(document, container, "p", "file-preview-empty", "该文件没有内容（0 字节）。");
      return;
    }
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
    if (model.openPath) {
      const open = appendText(document, actions, "button", "text-button", "在系统中打开");
      open.type = "button";
      open.dataset.action = "open-preview-in-system";
      open.dataset.filePath = model.openPath;
      const download = appendText(document, actions, "button", "text-button", "下载");
      download.type = "button";
      download.dataset.action = "download-preview-file";
      download.dataset.filePath = model.openPath;
    } else {
      appendText(document, actions, "span", "muted tiny", "该文件没有可用于系统打开的绝对路径");
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
  // 图片缺少可用地址时改走信息卡：否则会产出指向空标识的图像请求。
  function mount(container, shelf, view) {
    if (!container) return;
    const document = container.ownerDocument || global.document;
    if (!document) return;
    container.replaceChildren();
    const current = view && view.kind ? view : null;
    if (!current) return;
    renderHeader(container, document, current);
    const render = current.kind === "image" && !current.url
      ? renderBinaryCard
      : RENDERERS[current.kind] || renderBinaryCard;
    render(container, document, current);
  }

  global.focusFilePreview = {
    classify,
    decodeText,
    mediaTypeForName,
    createShelf,
    createObjectUrls,
    renderLineNumbers,
    binaryCardModel,
    mount,
  };
})(window);
