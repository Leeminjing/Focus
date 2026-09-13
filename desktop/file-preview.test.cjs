/*
 * 本文件验证会话页原生文件预览的宿主归属地。输入为文件名、文件记录与预览视图，
 * 输出为分类结果、标签页集合行为、行号拆分与信息卡模型，以及容器内的预览 DOM 结构断言；
 * 工作流不访问网络、不依赖真实浏览器 DOM。
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// 最小 DOM 替身：只实现 mount 与信息卡实际用到的那部分接口
function createElement(document, tagName, parent) {
  const node = {
    ownerDocument: document,
    tagName: String(tagName).toUpperCase(),
    className: "",
    textContent: "",
    dataset: {},
    children: [],
    attributes: {},
    parentNode: parent || null,
    src: "",
    alt: "",
    title: "",
    type: "",
    appendChild(child) {
      child.parentNode = node;
      node.children.push(child);
      return child;
    },
    replaceChildren(...nodes) {
      node.children = [];
      for (const child of nodes) node.appendChild(child);
    },
    setAttribute(name, value) { node.attributes[name] = String(value); },
    querySelector(selector) { return find(node, selector); },
    querySelectorAll(selector) { return collect(node, selector); },
  };
  return node;
}

function matchesSimple(node, part) {
  if (!part) return false;
  if (part.startsWith(".")) return node.className.split(/\s+/).includes(part.slice(1));
  return node.tagName === part.toUpperCase();
}

// 支持 mount/信息卡用到的祖先-后代复合选择器（如 ".file-preview-binary-meta dd"）
function matches(node, selector) {
  const parts = String(selector).trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return false;
  if (!matchesSimple(node, parts[parts.length - 1])) return false;
  let ancestor = node.parentNode;
  for (let index = parts.length - 2; index >= 0; index -= 1) {
    while (ancestor && !matchesSimple(ancestor, parts[index])) ancestor = ancestor.parentNode;
    if (!ancestor) return false;
    ancestor = ancestor.parentNode;
  }
  return true;
}

function collect(root, selector) {
  const found = [];
  for (const child of root.children || []) {
    if (matches(child, selector)) found.push(child);
    found.push(...collect(child, selector));
  }
  return found;
}

function find(root, selector) {
  return collect(root, selector)[0] || null;
}

function createDocument() {
  const document = { createElement: tagName => createElement(document, tagName) };
  return document;
}

const context = vm.createContext({});
context.window = context;
context.document = createDocument();
new vm.Script(
  fs.readFileSync(require.resolve("./file-preview.js"), "utf8")
).runInContext(context);
const preview = context.focusFilePreview;

const plain = value => JSON.parse(JSON.stringify(value));
const doc = createDocument();
const mountInto = view => {
  const container = createElement(doc, "div");
  preview.mount(container, null, view);
  return container;
};

// === 分类值域封闭 ===

assert.equal(preview.classify("a.png"), "image");
assert.equal(preview.classify("a.JPG"), "image");
assert.equal(preview.classify("a.jpeg"), "image");
assert.equal(preview.classify("a.webp"), "image");
assert.equal(preview.classify("a.svg"), "image");
assert.equal(preview.classify("a.md"), "markdown");
assert.equal(preview.classify("a.markdown"), "markdown");
assert.equal(preview.classify("a.MD"), "markdown");
assert.equal(preview.classify("a.pdf"), "pdf");
assert.equal(preview.classify("a.txt"), "text");
assert.equal(preview.classify("a.PY"), "text");
assert.equal(preview.classify("a.tsx"), "text");

// 未知、无后缀、多点后缀、异常输入一律兜底为 binary，绝不返回空
assert.equal(preview.classify("a.docx"), "binary");
assert.equal(preview.classify("a.xlsx"), "binary");
assert.equal(preview.classify("a.zip"), "binary");
assert.equal(preview.classify("archive.tar.gz"), "binary");
assert.equal(preview.classify("noextension"), "binary");
assert.equal(preview.classify("trailing."), "binary");
assert.equal(preview.classify(".gitignore"), "binary");
assert.equal(preview.classify(""), "binary");
assert.equal(preview.classify(null), "binary");
assert.equal(preview.classify(undefined), "binary");
assert.equal(preview.classify("dir/sub/note.md"), "markdown");
assert.equal(preview.classify("C:\\work\\note.md"), "markdown");
assert.equal(preview.classify("note.md?rev=2"), "markdown");

const everyKind = ["image", "markdown", "text", "pdf", "binary"];
for (const name of ["a.png", "a.md", "a.txt", "a.pdf", "a.docx", "x", ""]) {
  assert.ok(everyKind.includes(preview.classify(name)), `${name} 必须落在值域内`);
}

// === 标签页集合状态机 ===

const shelf = preview.createShelf();
const fileA = { material_id: "m1", relative_path: "notes/a.md" };
const fileB = { material_id: "m2", relative_path: "notes/b.txt" };
const fileC = { material_id: "m3", relative_path: "notes/c.png" };

assert.equal(shelf.isEmpty(), true);
assert.equal(shelf.active(), null);
assert.equal(shelf.size, 0);

assert.equal(shelf.open(fileA).opened, true);
assert.equal(shelf.isEmpty(), false);
assert.equal(shelf.active().material_id, "m1");

// 重复打开同一文件：不产生重复标签页，并切换过去
assert.equal(shelf.open(fileB).opened, true);
assert.equal(shelf.active().material_id, "m2");
assert.equal(shelf.open(fileA).opened, false);
assert.equal(shelf.size, 2);
assert.equal(shelf.active().material_id, "m1");
assert.deepEqual(plain(shelf.list().map(item => item.material_id)), ["m1", "m2"]);

// 按文件名也能解析已打开项
assert.equal(shelf.resolve({ relative_path: "notes/a.md" }), "m1");
assert.equal(shelf.resolve({ relative_path: "notes/missing.md" }), "");
assert.equal(shelf.has(fileB), true);
assert.equal(shelf.open(fileB).opened, false);

// 关闭非当前标签页不改变当前选择
assert.equal(shelf.close(fileB), true);
assert.equal(shelf.active().material_id, "m1");
assert.equal(shelf.size, 1);
assert.equal(shelf.close(fileB), false);

// 关闭当前标签页时接管相邻项
shelf.open(fileB);
shelf.open(fileC);
assert.equal(shelf.active().material_id, "m3");
shelf.close(fileC);
assert.equal(shelf.active().material_id, "m2");

// 关闭最后一个标签页后集合为空，宿主据此隐藏整列
shelf.close(fileB);
shelf.close(fileA);
assert.equal(shelf.isEmpty(), true);
assert.equal(shelf.active(), null);

// 无标识记录的打开请求被拒绝且不污染集合
assert.equal(shelf.open({}).opened, false);
assert.equal(shelf.isEmpty(), true);
shelf.open(fileA);
assert.equal(shelf.activate({ relative_path: "notes/missing.md" }), false);
assert.equal(shelf.active().material_id, "m1");

// === 行号拆分 ===

assert.deepEqual(plain(preview.renderLineNumbers("a\nb")), [
  { number: 1, text: "a" },
  { number: 2, text: "b" },
]);
assert.deepEqual(plain(preview.renderLineNumbers("a\r\nb\rc")), [
  { number: 1, text: "a" },
  { number: 2, text: "b" },
  { number: 3, text: "c" },
]);
assert.deepEqual(plain(preview.renderLineNumbers("")), [{ number: 1, text: "" }]);
assert.deepEqual(plain(preview.renderLineNumbers("a\n")), [
  { number: 1, text: "a" },
  { number: 2, text: "" },
]);

// === 信息卡模型 ===

assert.deepEqual(
  plain(preview.binaryCardModel({ material_id: "m9", relative_path: "docs/report.docx", size_bytes: 2048 })),
  { name: "report.docx", path: "docs/report.docx", suffix: "docx", sizeBytes: 2048, isImage: false, actionable: true }
);
assert.equal(preview.binaryCardModel({ relative_path: "x.bin" }).sizeBytes, 0);
assert.equal(preview.binaryCardModel({ relative_path: "x.bin" }).actionable, false);
assert.equal(preview.binaryCardModel(null).name, "");

// === 渲染分派 ===

const textView = mountInto({ kind: "text", text: "第一行\n第二行" });
const rows = textView.querySelectorAll(".file-preview-line");
assert.equal(rows.length, 2);
assert.equal(rows[0].querySelector(".file-preview-line-number").textContent, "1");
assert.equal(rows[1].querySelector(".file-preview-line-body").textContent, "第二行");

const markdownView = mountInto({ kind: "markdown", html: "<h1>标题</h1>" });
assert.equal(markdownView.querySelector(".file-preview-markdown").innerHTML, "<h1>标题</h1>");

const imageView = mountInto({ kind: "image", url: "/api/thumb/1", alt: "图" });
const imageNode = imageView.querySelector(".file-preview-image");
assert.equal(imageNode.src, "/api/thumb/1");
assert.equal(imageNode.alt, "图");

const pdfView = mountInto({ kind: "pdf", url: "/api/file/1" });
assert.equal(pdfView.querySelector(".file-preview-document").src, "/api/file/1");

const binaryView = mountInto({ kind: "binary", item: { material_id: "m9", relative_path: "docs/report.docx", size_bytes: 2048 } });
assert.equal(binaryView.querySelector(".file-preview-binary-name").textContent, "report.docx");
const metaText = binaryView.querySelectorAll(".file-preview-binary-meta dd").map(node => node.textContent);
assert.deepEqual(plain(metaText), [".docx", "2048 B", "docs/report.docx"]);
const actions = binaryView.querySelectorAll(".file-preview-binary-actions button").map(node => node.dataset.action);
assert.deepEqual(plain(actions), ["open-preview-in-system", "download-preview-file"]);

// 未登记为材料的文件仍有名称与类型说明，但不给无法兑现的出路口
const namelessView = mountInto({ kind: "binary", item: { relative_path: "mystery.docx" } });
assert.equal(namelessView.querySelector(".file-preview-binary-name").textContent, "mystery.docx");
assert.equal(namelessView.querySelectorAll(".file-preview-binary-actions button").length, 0);
assert.equal(namelessView.querySelectorAll(".file-preview-binary-actions .muted").length, 1);

// 视图缺失时容器为空，由宿主负责隐藏整列
const emptyView = mountInto(null);
assert.equal(emptyView.children.length, 0);
assert.equal(mountInto({ kind: "unknown-kind" }).querySelector(".file-preview-binary") !== null, true);

// 重复 mount 不累积节点
const reused = createElement(doc, "div");
preview.mount(reused, null, { kind: "text", text: "x" });
preview.mount(reused, null, { kind: "text", text: "x" });
assert.equal(reused.querySelectorAll(".file-preview-text").length, 1);

console.log("file-preview: all assertions passed");
