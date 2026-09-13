/*
 * 本文件验证插件前端资源宿主，即「启停插件无需重载页面」所依赖的就地安装与卸载。
 * 输入为插件清单与假 document / 视图注册表，输出为注入顺序、节点增删、脚本记忆化清理、
 * 视图注册精确回收与对账幂等的断言；工作流不联网、不需要真实浏览器。
 * 示例：`node plugin-assets.test.cjs`
 */
"use strict";

const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readAppSource } = require("./test-helper.cjs");

// 抽出宿主函数单独执行：只依赖传入的 document 与 registry，不触碰模块其余状态
const source = readAppSource().match(/function createPluginAssetHost[\s\S]*?\n}/)?.[0];
assert.ok(source, "应能从 app.js 抽离插件前端资源宿主");

// 假 document：记录 head 下的节点，并模拟「<script> 追加后解析执行」
function createDocument(onScript) {
  const head = {
    children: [],
    append(node) {
      head.children.push(node);
      if (node.tagName !== "SCRIPT") return;
      onScript?.(node);
      node.onload?.();
    },
  };
  const document = {
    head,
    createElement(tagName) {
      return {
        tagName: String(tagName).toUpperCase(),
        remove() { head.children = head.children.filter(item => item !== this); },
      };
    },
    querySelector(selector) {
      const src = /^script\[src="(.*)"\]$/.exec(selector)?.[1];
      return head.children.find(node => node.tagName === "SCRIPT" && node.src === src) || null;
    },
  };
  return { document, head, names: () => head.children.map(node => `${node.tagName}:${(node.src || node.href || "").split("/").pop()}`) };
}

function createHost(document, registry) {
  const context = vm.createContext({ console });
  new vm.Script(`${source}\nglobalThis.__createHost = createPluginAssetHost;`).runInContext(context);
  return context.__createHost(document, registry);
}

// 跨 VM realm 的数组原型不同，比较前先归一为宿主 realm 的普通值
const plain = value => JSON.parse(JSON.stringify(value));

const plugin = (overrides = {}) => ({
  name: "spatial", version: "0.1", status: "active", can_toggle: true,
  desktop_assets: ["style.css", "viewer.js", "entry.js"], ...overrides,
});

// 插件的注册副作用：执行每个脚本时往 registry 写一个条目，模拟 entry.js 注册视图
function registeringFixture() {
  const registry = {};
  const executed = [];
  const fake = createDocument(node => {
    const file = node.src.split("/").pop();
    executed.push(file);
    registry[`view-from-${file}`] = { file };
  });
  return { registry, executed, fake, host: createHost(fake.document, registry) };
}

async function main() {
  // === 安装：样式先于脚本，entry.js 最后执行 ===

  const first = registeringFixture();
  await first.host.reconcile([plugin()]);
  assert.deepEqual(
    first.fake.names(),
    ["LINK:style.css", "SCRIPT:viewer.js", "SCRIPT:entry.js"],
    "样式必须先注入，且 entry.js 必须在其余脚本之后执行"
  );
  assert.deepEqual(plain(first.host.installed()), ["spatial"]);
  assert.ok(Object.keys(first.registry).length > 0, "安装后会注册视图");

  // === 卸载：节点移除、视图注册精确回收 ===

  first.host.uninstall("spatial");
  assert.deepEqual(first.fake.head.children, [], "卸载后不得残留 <link>/<script> 节点");
  assert.deepEqual(Object.keys(first.registry), [], "卸载后不得残留该插件的视图注册");
  assert.deepEqual(plain(first.host.installed()), []);

  // === 重新启用：脚本必须重新执行（记忆化已清除）===
  // 若卸载未清除记忆化，这里会复用旧 Promise，插件视图永远回不来

  first.executed.length = 0;
  await first.host.reconcile([plugin()]);
  assert.deepEqual(
    first.executed,
    ["viewer.js", "entry.js"],
    "重新启用必须重新执行插件脚本，证明记忆化已清除"
  );
  assert.ok(Object.keys(first.registry).length > 0, "重新启用后视图注册应当恢复");

  // === 对账：非 active 只卸载不安装；重复对账不重复注入 ===

  const second = registeringFixture();
  await second.host.reconcile([plugin()]);
  const installedOnce = second.fake.head.children.length;
  await second.host.reconcile([plugin()]);
  assert.equal(second.fake.head.children.length, installedOnce, "重复对账不得重复注入资源");

  await second.host.reconcile([plugin({ status: "disabled", can_toggle: true })]);
  assert.deepEqual(second.fake.head.children, [], "转为停用后应卸载其资源");
  await second.host.reconcile([]);
  assert.deepEqual(plain(second.host.installed()), []);

  // 非 active 插件从不安装
  const never = registeringFixture();
  await never.host.reconcile([plugin({ status: "unavailable" })]);
  assert.deepEqual(never.fake.head.children, [], "不可用插件不得注入任何资源");

  // === 多插件互不影响：卸载其一不触碰另一个 ===

  const multi = registeringFixture();
  const other = { name: "demo", version: "1.0", status: "active", can_toggle: true, desktop_assets: ["demo.css"] };
  await multi.host.reconcile([plugin(), other]);
  assert.deepEqual(plain(multi.host.installed()).sort(), ["demo", "spatial"]);
  multi.host.uninstall("spatial");
  assert.deepEqual(plain(multi.host.installed()), ["demo"]);
  assert.deepEqual(
    multi.fake.names(),
    ["LINK:demo.css"],
    "卸载 spatial 不得移除 demo 的样式"
  );

  // === 未声明前端资源的插件不产生副作用 ===

  const bare = registeringFixture();
  await bare.host.reconcile([{ name: "bare", version: "1", status: "active", can_toggle: true }]);
  assert.deepEqual(bare.fake.head.children, []);
  assert.deepEqual(plain(bare.host.installed()), ["bare"]);

  console.log("plugin-assets: all assertions passed");
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
