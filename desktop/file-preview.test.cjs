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

const context = vm.createContext({ TextDecoder, Blob });
context.window = context;
context.document = createDocument();
new vm.Script(
  fs.readFileSync(require.resolve("./file-preview.js"), "utf8")
).runInContext(context);
const preview = context.focusFilePreview;

const plain = value => JSON.parse(JSON.stringify(value));
// 容器必须用模块所见的同一个 document 创建，否则 mount 取不到可用 document
const doc = context.document;
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
  plain(preview.binaryCardModel({ material_id: "m9", path: "C:\\ws\\docs\\report.docx", relative_path: "docs/report.docx", size_bytes: 2048 })),
  { name: "report.docx", path: "docs/report.docx", openPath: "C:\\ws\\docs\\report.docx", suffix: "docx", sizeBytes: 2048, isImage: false }
);
assert.equal(preview.binaryCardModel({ relative_path: "x.bin" }).sizeBytes, 0);
assert.equal(preview.binaryCardModel({ relative_path: "x.bin" }).openPath, "");
assert.equal(preview.binaryCardModel(null).name, "");

// === 后缀 → Blob 地址用的媒体类型 ===

assert.equal(preview.mediaTypeForName("shots/a.png"), "image/png");
assert.equal(preview.mediaTypeForName("shots/a.JPG"), "image/jpeg");
assert.equal(preview.mediaTypeForName("shots/a.svg"), "image/svg+xml");
assert.equal(preview.mediaTypeForName("reports/a.pdf"), "application/pdf");
assert.equal(preview.mediaTypeForName("deep/dir/reports/a.pdf"), "application/pdf");
assert.equal(preview.mediaTypeForName("a.docx"), "");
assert.equal(preview.mediaTypeForName("noextension"), "");

// === 文本解码：与后端编码链同序（UTF-8 → GB18030） ===

const GBK_TABLE = { 中: [0xd6, 0xd0], 文: [0xce, 0xc4], 测: [0xb2, 0xe2], 试: [0xca, 0xd4] };
const encode = (text, encoding) => encoding === "gb18030"
  ? new Uint8Array([...text].flatMap(char => GBK_TABLE[char] || [0x3f]))
  : new TextEncoder().encode(text);

assert.deepEqual(plain(preview.decodeText(encode("中文测试", "utf-8"))), { text: "中文测试", encoding: "utf-8" });
assert.deepEqual(plain(preview.decodeText(encode("中文测试", "gb18030"))), { text: "中文测试", encoding: "gb18030" });
assert.deepEqual(plain(preview.decodeText(new Uint8Array(0))), { text: "", encoding: "utf-8" });
// 两种编码都无法解码时不抛错，而是给出尽力而为的正文
const undecodable = preview.decodeText(new Uint8Array([0x81]));
assert.equal(undecodable.encoding, "utf-8/replace");
assert.equal(typeof undecodable.text, "string");

// === 由字节构造的地址必须被回收 ===

const revoked = [];
const urls = preview.createObjectUrls({
  createObjectURL: blob => `blob:test/${blob.type}/${revoked.length}`,
  revokeObjectURL: url => revoked.push(url),
});
const firstUrl = urls.urlFor("k1", new Uint8Array([1]), "image/png");
const secondUrl = urls.urlFor("k1", new Uint8Array([2]), "image/png");
assert.notEqual(firstUrl, secondUrl);
assert.deepEqual(revoked, [firstUrl], "同一键重新构造地址时必须先回收旧地址");
assert.equal(urls.revoke("k1"), true);
assert.deepEqual(revoked, [firstUrl, secondUrl]);
assert.equal(urls.revoke("k1"), false, "重复回收应报告未命中");
assert.deepEqual(plain(urls.keys()), []);

// === 渲染分派 ===

const textView = mountInto({ kind: "text", text: "第一行\n第二行", item: { relative_path: "logs/run.log", size_bytes: 2048 } });
const rows = textView.querySelectorAll(".file-preview-line");
assert.equal(rows.length, 2);
assert.equal(rows[0].querySelector(".file-preview-line-number").textContent, "1");
assert.equal(rows[1].querySelector(".file-preview-line-body").textContent, "第二行");
// 统一头部：名称与大小不依赖标签页即可确认正在看的文件
assert.equal(textView.querySelector(".file-preview-name").textContent, "run.log");
assert.equal(textView.querySelector(".file-preview-size").textContent, "2.0 KB");

// 截断与非默认编码必须在正文之外可见
const flagged = mountInto({ kind: "text", text: "abc", item: { relative_path: "big.log" }, meta: { truncated: true, encoding: "gb18030" } });
const flags = flagged.querySelectorAll(".file-preview-flag").map(node => node.textContent);
assert.deepEqual(plain(flags), ["内容已截断", "gb18030"]);

// 空文件给明确提示，而不是只有一个行号的空框
const emptyText = mountInto({ kind: "text", text: "", item: { relative_path: "empty.txt", size_bytes: 0 } });
assert.equal(emptyText.querySelectorAll(".file-preview-line").length, 0);
assert.match(emptyText.querySelector(".file-preview-empty").textContent, /没有内容/);

const markdownView = mountInto({ kind: "markdown", html: "<h1>标题</h1>", item: { relative_path: "notes/a.md" } });
assert.equal(markdownView.querySelector(".file-preview-markdown").innerHTML, "<h1>标题</h1>");

const imageView = mountInto({ kind: "image", url: "/api/thumb/1", alt: "图", item: { relative_path: "shots/a.png" } });
const imageNode = imageView.querySelector(".file-preview-image");
assert.equal(imageNode.src, "/api/thumb/1");
assert.equal(imageNode.alt, "图");

// 图片缺少可用地址时改走信息卡，不产出指向空标识的图像请求
const urlLessImage = mountInto({ kind: "image", url: "", item: { relative_path: "shots/a.png" } });
assert.equal(urlLessImage.querySelector(".file-preview-image"), null);
assert.equal(urlLessImage.querySelector(".file-preview-binary") !== null, true);

const pdfView = mountInto({ kind: "pdf", url: "/api/file/1", item: { relative_path: "reports/a.pdf" } });
assert.equal(pdfView.querySelector(".file-preview-document").src, "/api/file/1");

const binaryView = mountInto({ kind: "binary", item: { material_id: "m9", path: "C:\\ws\\docs\\report.docx", relative_path: "docs/report.docx", size_bytes: 2048 } });
// 名称只在统一头部出现一次，卡片内不再重复
assert.equal(binaryView.querySelectorAll(".file-preview-name").length, 1);
assert.equal(binaryView.querySelector(".file-preview-name").textContent, "report.docx");
assert.equal(binaryView.querySelectorAll(".file-preview-binary strong").length, 0);
const metaText = binaryView.querySelectorAll(".file-preview-binary-meta dd").map(node => node.textContent);
assert.deepEqual(plain(metaText), ["report.docx", ".docx", "2.0 KB", "docs/report.docx"]);
const actions = binaryView.querySelectorAll(".file-preview-binary-actions button").map(node => node.dataset.action);
assert.deepEqual(plain(actions), ["open-preview-in-system", "download-preview-file"]);
// 「在系统中打开」必须携带绝对路径
const openButton = binaryView.querySelectorAll(".file-preview-binary-actions button")[0];
assert.equal(openButton.dataset.filePath, "C:\\ws\\docs\\report.docx");
// 大小以人类可读单位呈现，而不是裸字节数
assert.equal(binaryView.querySelector(".file-preview-size").textContent, "2.0 KB");

// 降级原因必须在正文可见，而不是只给一张沉默的卡片
const noted = mountInto({ kind: "binary", item: { relative_path: "x.md" }, note: "无法预览该文件：仅接受绝对路径" });
assert.match(noted.querySelector(".file-preview-empty").textContent, /仅接受绝对路径/);

// 没有绝对路径时不给无法兑现的出路口
const noPathView = mountInto({ kind: "binary", item: { relative_path: "mystery.docx" } });
assert.equal(noPathView.querySelector(".file-preview-binary-meta dd").textContent, "mystery.docx");
assert.equal(noPathView.querySelectorAll(".file-preview-binary-actions button").length, 0);
assert.equal(noPathView.querySelectorAll(".file-preview-binary-actions .muted").length, 1);

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
