/*
 * 本文件验证压缩块内的图片材料缩略图与点击放大。输入为压缩分界项（含被压缩来源），输出为
 * 缩略图行、材料内容地址与放大动作属性的断言；工作流不访问网络或真实 DOM。
 */
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

function renderDivider(item) {
  const harness = createAppHarness();
  harness.vm.runInContext(readAppSource(), harness.context);
  return harness.vm.runInContext(
    `renderCompressionDivider(${JSON.stringify(item)})`, harness.context
  );
}

const imageRef = id => `看这张【图片1 material_id=${id}】`;
const human = content => ({ role: "human", content });

// 含图片的压缩块：渲染缩略图，并带上材料内容地址与放大动作
let html = renderDivider({
  block_id: "b1",
  summary: "摘要",
  count: 1,
  source: [human(imageRef("m1"))],
});
assert.match(html, /compression-block-images/, "压缩块渲染缩略图行");
assert.match(html, /class="compression-block-image"/, "渲染缩略图元素");
assert.match(html, /\/desktop\/api\/materials\/m1\/content/, "缩略图指向材料内容接口");
assert.match(html, /data-action="zoom-image"/, "缩略图可点击放大");
assert.match(html, /data-image-url="[^"]*materials\/m1\/content[^"]*"/, "放大动作携带图片地址");

// 纯文本压缩块：不得出现任何缩略图占位
html = renderDivider({ block_id: "b2", summary: "摘要", count: 1, source: [human("只有文字")] });
assert.doesNotMatch(html, /compression-block-image/, "纯文本压缩块无缩略图");
assert.doesNotMatch(html, /compression-block-images/, "纯文本压缩块无缩略图容器");

// 同一张图片被引用多次只渲染一次
html = renderDivider({
  block_id: "b3",
  summary: "摘要",
  count: 2,
  source: [human(imageRef("m1")), human(`再来一次【图片1 material_id=m1】`)],
});
assert.equal(html.match(/class="compression-block-image"/g).length, 1, "同一材料去重");

// 嵌套压缩块：递归取出内层来源里的图片
html = renderDivider({
  block_id: "b4",
  summary: "摘要",
  count: 1,
  source: [{ role: "ai", content: "外层文字" }, { compression: { source: [human(imageRef("m9"))] } }],
});
assert.match(html, /materials\/m9\/content/, "递归取出嵌套来源里的图片");

// 删除墓碑只提示，不渲染缩略图
html = renderDivider({ block_id: "b5", summary: "", count: 1, deleted: true, source: [human(imageRef("m1"))] });
assert.doesNotMatch(html, /compression-block-image/, "删除墓碑不渲染缩略图");
assert.match(html, /已删除/, "删除墓碑保留既有提示");

// 来源缺失时不抛异常
html = renderDivider({ block_id: "b6", summary: "摘要", count: 0 });
assert.match(html, /compression-block-summary/, "来源缺失仍渲染摘要");
assert.doesNotMatch(html, /compression-block-image/, "来源缺失无缩略图");

console.log("compression-block-images: all assertions passed");
