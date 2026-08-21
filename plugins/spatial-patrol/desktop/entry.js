/* spatial-patrol 插件前端入口:注册视图与材料打开器到系统宿主。
   app.js 按启用清单注入本脚本后,经 window.__focusPluginViews 注册表接入。 */
(function (root) {
  "use strict";

  const host = (root.__focusPluginViews = root.__focusPluginViews || {});
  const viewer = root.FocusSpatialViewer;
  if (!viewer) return;

  host["spatial-viewer"] = {
    render: (app, appState) => viewer.render(app, appState),
    openMaterial: (material, appState) => viewer.openMaterial(material, appState),
    supportsMaterial: material => viewer.supportsMaterial(material),
    mountPanel: (container, material, appState) => viewer.mountPanel(container, material, appState),
    getFocus: () => viewer.getFocus(),
    sendFocusedMessage: message => viewer.sendFocusedMessage(message),
  };
})(typeof globalThis === "object" ? globalThis : this);
