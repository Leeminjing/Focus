/*
 * 本文件验证旧图片引用兼容与新逐轮材料草稿纯函数。输入为任意材料、有序绑定、备注缓存和
 * 图片必看集合；输出为旧引用可读、新载荷结构、取消再勾选备注恢复及子集不变量断言。
 * 具体工作流独立加载两个无 DOM 模块并比较可序列化结果。示例：node --test image-material-picker.test.cjs。
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const context = vm.createContext({});
context.window = context;
for (const file of ["./image-material-picker.js", "./run-material-picker.js"]) {
  new vm.Script(fs.readFileSync(require.resolve(file), "utf8")).runInContext(context);
}
const legacy = context.imageMaterialPicker;
const picker = context.runMaterialPicker;
const plain = value => JSON.parse(JSON.stringify(value));
const image = (id, size = 1024) => ({ material_id: id, relative_path: `${id}.png`, is_image: true, size_bytes: size });
const text = id => ({ material_id: id, relative_path: `${id}.md`, is_image: false, size_bytes: 10 });
const materials = [image("m1"), text("m2"), image("empty", 0)];

assert.equal(legacy.formatMaterialRef(1, "m1"), "【图片1 material_id=m1】");
assert.deepEqual(plain(legacy.materialRefIds("a【图片1 material_id=m1】【图片2 material_id=m1】")), ["m1"]);
assert.equal(legacy.stripMaterialRefs("a【图片1 material_id=m1】"), "a");

let selection = picker.empty();
selection = picker.toggleMaterial(selection, "m2");
selection = picker.setNote(selection, "m2", "只看第三章\n保留原文 <tag>");
selection = picker.toggleMaterial(selection, "m1");
selection = picker.toggleRequired(selection, "m1", materials);
assert.deepEqual(plain(picker.buildOutgoing(materials, "比较", selection)), {
  message: "比较",
  materialInputs: [
    { material_id: "m2", note: "只看第三章\n保留原文 <tag>" },
    { material_id: "m1", note: "" },
  ],
  requiredImageIds: ["m1"],
});

selection = picker.toggleMaterial(selection, "m2");
selection = picker.toggleMaterial(selection, "m2");
assert.equal(selection.bindings.at(-1).note, "只看第三章\n保留原文 <tag>");
assert.deepEqual(plain(picker.toggleRequired(selection, "m2", materials).requiredImageIds), ["m1"]);
assert.deepEqual(plain(picker.toggleRequired(selection, "empty", materials).requiredImageIds), ["m1"]);

const normalized = picker.normalizeSelection(selection, [image("m1")]);
assert.deepEqual(plain(normalized.bindings.map(item => item.materialId)), ["m1"]);
assert.deepEqual(plain(normalized.requiredImageIds), ["m1"]);
assert.equal(Object.hasOwn(normalized.notes, "m2"), false);

console.log("run-material-picker: all assertions passed");
