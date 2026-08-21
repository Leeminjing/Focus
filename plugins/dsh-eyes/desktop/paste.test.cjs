/*
 * 本文件验证 dsh-eyes Composer 附件条。输入为模拟的图片剪贴板项，输出为入队、单张移除、
 * 发送取走清空及零内联样式断言；工作流使用最小原生 DOM 替身，不访问文件系统或网络。
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync(__dirname + "/paste.js", "utf8");
assert.doesNotMatch(source, /style\s*=|\.style\b/, "附件预览不得写入内联样式");

let pasteHandler;
let preview;
const removeListeners = [];
const parent = { insertBefore(node) { preview = node; node.isConnected = true; } };
const composer = { parentElement: parent };
const input = { closest(selector) { return selector === "#mainInput" ? input : selector === ".composer" ? composer : null; } };

const document = {
  addEventListener(type, handler) { if (type === "paste") pasteHandler = handler; },
  createElement() {
    return {
      className: "",
      isConnected: false,
      remove() { this.isConnected = false; },
      set innerHTML(value) { this.html = value; },
      get innerHTML() { return this.html || ""; },
      querySelectorAll() {
        if (!this.innerHTML.includes("data-remove-image")) return [];
        return [{ dataset: { removeImage: "0" }, addEventListener(type, handler) { if (type === "click") removeListeners.push(handler); } }];
      },
    };
  },
};

class FileReader {
  readAsDataURL() { this.result = "data:image/png;base64,AA=="; this.onload(); }
}

const context = vm.createContext({ document, FileReader });
new vm.Script(source).runInContext(context);

let prevented = false;
pasteHandler({
  target: input,
  preventDefault() { prevented = true; },
  clipboardData: { items: [{ kind: "file", type: "image/png", getAsFile: () => ({ name: "paste.png" }) }] },
});

assert.equal(prevented, true);
assert.equal(context.__dshEyesPendingImages.length, 1);
assert.equal(preview.className, "dsh-eyes-paste-preview");
assert.match(preview.innerHTML, /dsh-eyes-preview-image/);
assert.match(preview.innerHTML, /aria-label="移除第 1 张图片"/);

removeListeners.at(-1)();
assert.equal(context.__dshEyesPendingImages.length, 0, "单张移除更新队列");

pasteHandler({
  target: input,
  preventDefault() {},
  clipboardData: { items: [{ kind: "file", type: "image/png", getAsFile: () => ({ name: "again.png" }) }] },
});
const taken = context.__dshEyesTakePendingImages();
assert.equal(taken.length, 1);
assert.equal(context.__dshEyesPendingImages.length, 0, "发送取走后清空队列");
assert.equal(preview.isConnected, false, "发送后移除附件条");

console.log("dsh-eyes paste: 队列、单张移除、发送清空与宿主样式接入通过");
