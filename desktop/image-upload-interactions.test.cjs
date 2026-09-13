/*
 * 本文件验证桌面图片输入事件边界。输入为主输入框、其它控件、剪贴板图片和可重复选择的文件
 * input；输出为仅主输入框拦截、多图顺序上传、纯文本默认粘贴和每次选择后重置断言。
 * 具体工作流调用 app.js 注册的 paste/change handler，并以可控上传函数观察调用顺序。
 * 示例：node --test desktop/image-upload-interactions.test.cjs。
 */
"use strict";

const assert = require("node:assert/strict");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

const mainInput = { id: "mainInput", value: "" };
const harness = createAppHarness({
  selectors: { "#mainInput": mainInput },
  globals: { queueMicrotask },
});
harness.vm.runInContext(readAppSource(), harness.context);

const paste = harness.listeners.get("paste").at(-1);
const change = harness.listeners.get("change").at(-1);
const imageItem = name => ({ type: "image/png", getAsFile: () => ({ name }) });
const pasteEvent = (target, items) => ({
  target,
  clipboardData: { items },
  prevented: false,
  preventDefault() { this.prevented = true; },
});

(async () => {
  harness.vm.runInContext(`
    state.activeTaskId = "task";
    globalThis.__uploads = [];
    uploadMaterialFile = async file => {
      __uploads.push("start:" + file.name);
      await new Promise(resolve => queueMicrotask(resolve));
      __uploads.push("end:" + file.name);
    };
  `, harness.context);

  const foreign = pasteEvent({ id: "settingsInput" }, [imageItem("foreign.png")]);
  paste(foreign);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(foreign.prevented, false);
  assert.deepEqual(Array.from(harness.context.__uploads), []);

  const text = pasteEvent(mainInput, [{ type: "text/plain", getAsFile: () => null }]);
  paste(text);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(text.prevented, false);

  const images = pasteEvent(mainInput, [imageItem("first.png"), imageItem("second.png")]);
  paste(images);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(images.prevented, true);
  assert.deepEqual(Array.from(harness.context.__uploads), [
    "start:first.png", "end:first.png", "start:second.png", "end:second.png",
  ]);

  harness.context.__uploads.length = 0;
  const fileInput = { id: "fileInput", files: [{ name: "same.png" }], value: "C:/fake/same.png" };
  change({ target: fileInput });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(fileInput.value, "");
  fileInput.value = "C:/fake/same.png";
  change({ target: fileInput });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(fileInput.value, "");
  assert.deepEqual(Array.from(harness.context.__uploads), [
    "start:same.png", "end:same.png", "start:same.png", "end:same.png",
  ]);

  console.log("image-upload-interactions: all assertions passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
