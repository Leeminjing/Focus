/*
 * 本文件验证图片材料的「本轮必须看」判定与发送载荷组装。输入为材料 payload 与输入框文本，
 * 输出为可勾选性、勾选集对齐结果与 {message, mustViewIds} 载荷断言；工作流不访问网络或真实 DOM。
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const context = vm.createContext({});
context.window = context;
new vm.Script(
  fs.readFileSync(require.resolve("./image-material-picker.js"), "utf8")
).runInContext(context);
const picker = context.imageMaterialPicker;

// 跨 VM realm 的数组/对象原型不同，比较前先归一为宿主 realm 的普通值
const plain = value => JSON.parse(JSON.stringify(value));

const imageMaterial = (id, size = 1024) => ({
  material_id: id,
  relative_path: `.focus/attachments/${id}.png`,
  is_image: true,
  size_bytes: size,
});
const textMaterial = id => ({
  material_id: id,
  relative_path: `notes-${id}.md`,
  is_image: false,
  size_bytes: 2048,
});

// 引用标记与后端 focus/messages/material_refs.py 必须同形
assert.equal(picker.formatMaterialRef(1, "ab12"), "【图片1 material_id=ab12】");
assert.deepEqual(
  plain(picker.materialRefIds("看这张【图片1 material_id=ab12】再看【图片2 material_id=cd34】")),
  ["ab12", "cd34"]
);
assert.deepEqual(
  plain(picker.materialRefIds("看这张【图片1 material_id=ab12】再看【图片2 material_id=ab12】")),
  ["ab12"]
);
assert.equal(picker.stripMaterialRefs("看这张【图片1 material_id=ab12】"), "看这张");

// 只有非空图片材料可勾选
assert.equal(picker.canMustView(imageMaterial("m1")), true);
assert.equal(picker.canMustView(imageMaterial("m2", 0)), false);
assert.equal(picker.canMustView(textMaterial("m3")), false);
assert.equal(picker.canMustView(null), false);
assert.match(picker.mustViewBlockReason(textMaterial("m3")), /只适用于图片材料/);
assert.match(picker.mustViewBlockReason(imageMaterial("m2", 0)), /内容为空/);

// 勾选集与最新材料列表对齐：被删除或置空的材料自动失效
assert.deepEqual(
  plain(
    picker.syncMustView(
      ["m1", "m2", "m3"],
      [imageMaterial("m1"), imageMaterial("m2", 0), textMaterial("m3")]
    )
  ),
  ["m1"]
);
assert.deepEqual(plain(picker.syncMustView(["m9"], [imageMaterial("m1")])), []);
assert.deepEqual(plain(picker.syncMustView(undefined, [imageMaterial("m1")])), []);

// 未勾选时不产生引用标记，也不带必需清单
assert.deepEqual(plain(picker.buildOutgoing([textMaterial("m3")], "帮我看看")), {
  message: "帮我看看",
  mustViewIds: [],
});
assert.deepEqual(plain(picker.buildOutgoing([], "帮我看看")), {
  message: "帮我看看",
  mustViewIds: [],
});

// 勾选后引用标记追加到文本尾部，必需清单与标记一一对应
assert.deepEqual(plain(picker.buildOutgoing([imageMaterial("m1"), imageMaterial("m2")], "看这两张")), {
  message: "看这两张\n【图片1 material_id=m1】【图片2 material_id=m2】",
  mustViewIds: ["m1", "m2"],
});
assert.deepEqual(plain(picker.buildOutgoing([imageMaterial("m1")], "")), {
  message: "【图片1 material_id=m1】",
  mustViewIds: ["m1"],
});

// 组装出的载荷可被自身回读
const outgoing = plain(picker.buildOutgoing([imageMaterial("m1"), imageMaterial("m2")], "看这两张"));
assert.deepEqual(plain(picker.materialRefIds(outgoing.message)), outgoing.mustViewIds);

console.log("image-material-picker: all assertions passed");
