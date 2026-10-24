/*
 * 本文件验证自定义优先的材料分组纯函数。输入为材料、membership、run/type 模式、折叠 key
 * 和本轮选择；输出为唯一归属、稳定顺序、选择摘要与失效 key 清理断言。具体工作流独立加载
 * material-grouping.js，不创建 DOM 或请求。示例：node --test material-grouping.test.cjs。
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const context = vm.createContext({});
context.window = context;
new vm.Script(fs.readFileSync(require.resolve("./material-grouping.js"), "utf8")).runInContext(context);
const project = context.materialGrouping.project;
const plain = value => JSON.parse(JSON.stringify(value));
const materials = [
  { material_id: "m1", material_kind: "image", first_run_id: "r2" },
  { material_id: "m2", material_kind: "text", first_run_id: "r1" },
  { material_id: "m3", material_kind: "pdf", first_run_id: null },
];
const custom = [{ group_id: "g1", name: "人工", position: 0, memberships: [{ material_id: "m2", position: 0 }] }];
const selection = { bindings: [{ materialId: "m2" }, { materialId: "m3" }] };

const byRun = plain(project(materials, custom, "run", ["custom:g1", "gone:x"], selection));
assert.deepEqual(byRun.collapsedKeys, ["custom:g1"]);
assert.equal(byRun.groups[0].key, "custom:g1");
assert.equal(byRun.groups[0].selectedCount, 1);
assert.equal(byRun.groups[0].collapsed, true);
assert.deepEqual(byRun.groups.flatMap(group => group.materials.map(item => item.material_id)).sort(), ["m1", "m2", "m3"]);
assert.ok(byRun.groups.some(group => group.key === "run:r2"));
assert.ok(byRun.groups.some(group => group.key === "run:unassigned"));

const byType = plain(project(materials, custom, "type", [], selection));
assert.ok(byType.groups.some(group => group.key === "type:image"));
assert.ok(byType.groups.some(group => group.key === "type:pdf"));
assert.equal(byType.groups.some(group => group.key === "type:text"), false);

console.log("material-grouping: all assertions passed");
