/*
 * 本文件验证图片材料选择、独立必看控件与逐轮备注可见性。输入为任务材料、binding/required 状态、
 * 材料备注和可控 image-validity API；输出为三个合法 UI 状态、备注框只随本轮选择出现且与必看
 * 相互独立、选择时的新鲜度校验，以及缩略图/消息/压缩/放大四个入口不泄漏 session 的断言。具体
 * 工作流在 VM 中直接调用 app.js 的纯渲染和选择函数。
 * 示例：node desktop/image-material-ui.test.cjs。
 */
"use strict";

const assert = require("node:assert/strict");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

const overlay = { innerHTML: "" };
const harness = createAppHarness({ selectors: { "#overlayRoot": overlay } });
harness.vm.runInContext(readAppSource(), harness.context);

(async () => {
  const result = await harness.vm.runInContext(`(async () => {
    const material = {
      material_id: "m1", relative_path: "shot.png", is_image: true, size_bytes: 80,
    };
    state.activeTaskId = "task";
    state.materials.set("task", [material]);

    state.materialSelections.set("task", runMaterialPicker.empty());
    const idle = renderImageMaterial(material);
    state.materialSelections.set("task", { bindings: [{ materialId: "m1", note: "对照" }], requiredImageIds: [], notes: { m1: "对照" } });
    const attached = renderImageMaterial(material);
    state.materialSelections.set("task", { bindings: [{ materialId: "m1", note: "对照" }], requiredImageIds: ["m1"], notes: { m1: "对照" } });
    const required = renderImageMaterial(material);

    const calls = [];
    api = async path => { calls.push(path); return {}; };
    state.materialSelections.set("task", runMaterialPicker.empty());
    toggleRunMaterial("m1");
    await toggleImageRequired("m1");

    const message = renderMessageImages(["m1"]);
    const compression = renderCompressionBlockImages(["m1"]);
    openImageLightbox("blob:preview", "shot.png");
    return { idle, attached, required, calls, message, compression };
  })()`, harness.context);

  assert.match(result.idle, /toggle-run-material" aria-pressed="false"/);
  assert.match(result.idle, /toggle-image-required" aria-pressed="false" disabled/);
  assert.match(result.attached, /toggle-run-material" aria-pressed="true"/);
  assert.match(result.attached, /toggle-image-required" aria-pressed="false">/);
  assert.match(result.required, /toggle-image-required" aria-pressed="true">/);
  // 备注框只随本轮选择出现，并与「必须看」各自独立。
  assert.doesNotMatch(result.idle, /run-material-note/);
  assert.match(result.attached, /run-material-note[\s\S]*对照/);
  assert.match(result.required, /run-material-note[\s\S]*对照/);
  assert.equal(result.calls.length, 1);
  assert.ok(result.calls.every(path => path.endsWith("/materials/m1/image-validity")));

  for (const html of [result.idle, result.message, result.compression]) {
    assert.match(html, /data-material-task-id="task"/);
    assert.match(html, /data-material-content-id="m1"/);
    assert.doesNotMatch(html, /session=|focus-dev-session/);
  }
  assert.match(overlay.innerHTML, /src="blob:preview"/);
  assert.doesNotMatch(overlay.innerHTML, /session=|focus-dev-session/);

  console.log("image-material-ui: all assertions passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
