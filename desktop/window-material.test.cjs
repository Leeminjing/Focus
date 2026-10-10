/* 本文件对外提供原生材质能力与生命周期测试。输入为平台版本及模拟系统偏好；输出为配置、退化和清理断言。
 * 工作流为覆盖不支持平台、开启透明、减少透明、高对比和关闭；示例：node --test desktop/window-material.test.cjs。
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { supportsAcrylic, bindWindowMaterial } = require("./window-material.cjs");
test("Acrylic respects platform capability, accessibility and listener lifetime", () => {
  assert.equal(supportsAcrylic("win32", "10.0.22621"), true);
  for (const [platform, release] of [["linux","10.0.26200"],["win32","10.0.19045"],["win32","invalid"]]) assert.equal(supportsAcrylic(platform,release),false);
  const theme = new EventEmitter(); const win = new EventEmitter(); const state={};
  win.isDestroyed=()=>false;
  win.setBackgroundMaterial=value=>state.material=value;
  win.setBackgroundColor=value=>state.color=value;
  win.setTitleBarOverlay=value=>state.overlay=value;
  bindWindowMaterial(win,theme,false); assert.deepEqual(state,{});
  bindWindowMaterial(win,theme,true); assert.equal(state.material,"acrylic");
  theme.prefersReducedTransparency=true; theme.emit("updated"); assert.equal(state.material,"none"); assert.equal(state.color,"#f3f6fb");
  theme.prefersReducedTransparency=false; theme.shouldUseHighContrastColors=true; theme.emit("updated"); assert.equal(state.material,"none");
  theme.shouldUseHighContrastColors=false; theme.emit("updated"); assert.equal(state.material,"acrylic");
  win.emit("closed"); assert.equal(theme.listenerCount("updated"),0);
});
