/* 本文件对外提供 supportsAcrylic 与 bindWindowMaterial，供现有窗口入口配置系统背景。
 * 输入为平台/版本和 Electron window/nativeTheme；输出为能力布尔值与监听解绑函数。
 * 工作流为支持的 Windows 使用系统 Acrylic，减少透明或高对比时恢复实色，窗口关闭即清理监听。
 * 示例：bindWindowMaterial(win, nativeTheme, supportsAcrylic(process.platform, os.release()))。
 * 本模块不创建窗口、不采集桌面、不改变安全选项、业务状态或缩放。
 */
"use strict";
const SOLID = "#f3f6fb";
function supportsAcrylic(platform, release) {
  const [major, , build] = String(release).split(".").map(Number);
  return platform === "win32" && major >= 10 && build >= 22621;
}
function bindWindowMaterial(win, theme, supported) {
  if (!supported) return () => {};
  const update = () => {
    if (win.isDestroyed()) return;
    const solid = theme.prefersReducedTransparency || theme.shouldUseHighContrastColors;
    win.setBackgroundMaterial(solid ? "none" : "acrylic");
    win.setBackgroundColor(solid ? SOLID : "#00000000");
    win.setTitleBarOverlay({ color: solid ? SOLID : "#00000000", symbolColor: "#142039" });
  };
  const dispose = () => theme.removeListener("updated", update);
  update();
  theme.on("updated", update);
  win.once("closed", dispose);
  return dispose;
}
module.exports = { supportsAcrylic, bindWindowMaterial };
