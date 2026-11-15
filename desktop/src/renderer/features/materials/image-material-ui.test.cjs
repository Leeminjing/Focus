/*
 * 本文件验证图片材料选择和逐轮备注可见性。输入为任务材料、binding 状态、
 * 材料备注和可控 API；输出为备注框只随本轮选择出现、没有声明控件或额外校验，
 * 以及缩略图/消息/压缩/放大四个入口不泄漏 session 的断言。具体
 * 工作流在 VM 中直接调用 app.js 的纯渲染和选择函数。
 * 示例：node desktop/src/renderer/features/materials/image-material-ui.test.cjs。
 */
"use strict";

const assert = require("node:assert/strict");
const { createAppHarness, readAppSource } = require("../../../../tests/helpers/test-helper.cjs");

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
    const idle = renderMaterial(material);
    state.materialSelections.set("task", { bindings: [{ materialId: "m1", note: "对照" }], notes: { m1: "对照" } });
    const attached = renderMaterial(material);

    const calls = [];
    api = async path => { calls.push(path); return {}; };
    state.materialSelections.set("task", runMaterialPicker.empty());
    toggleRunMaterial("m1");

    const message = renderMessageImages(["m1"]);
    const compression = renderCompressionBlockImages(["m1"]);
    openImageLightbox("blob:preview", "shot.png");
    return { idle, attached, calls, message, compression };
  })()`, harness.context);

  assert.match(result.idle, /toggle-run-material" aria-pressed="false"/);
  assert.match(result.attached, /toggle-run-material" aria-pressed="true"/);
  // 备注框只随本轮选择出现，不依赖额外的图片声明。
  assert.doesNotMatch(result.idle, /run-material-note/);
  assert.match(result.attached, /run-material-note[\s\S]*对照/);
  assert.equal(result.calls.length, 0);
  assert.doesNotMatch(result.attached, /toggle-image-required|必须看/);

  for (const html of [result.idle, result.message, result.compression]) {
    assert.match(html, /data-material-task-id="task"/);
    assert.match(html, /data-material-content-id="m1"/);
    assert.doesNotMatch(html, /session=|focus-dev-session/);
  }
  assert.match(overlay.innerHTML, /src="blob:preview"/);
  assert.doesNotMatch(overlay.innerHTML, /session=|focus-dev-session/);

  console.log("image-material-ui: all assertions passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
