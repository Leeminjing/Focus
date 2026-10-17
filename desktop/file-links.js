/* Shared local-link identity for the workbench and robot. Paths come only from
 * href and are decoded once; the backend validates the owning task/run/slot. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.FocusFileLinks = api;
})(globalThis, function () {
  "use strict";
  function pathFromHref(href) {
    if (!href || href.startsWith("#") || /^https?:/i.test(href)) return null;
    if (/^[a-z][a-z\d+.-]*:/i.test(href) && !/^file:/i.test(href) && !/^[a-z]:[\\/]/i.test(href)) return null;
    let path = href.replace(/[?#].*$/, "");
    if (/^file:/i.test(path)) {
      path = path.replace(/^file:\/\/(localhost)?(?=\/)/i, "").replace(/^file:/i, "");
      if (/^\/[a-z]:[\\/]/i.test(path)) path = path.slice(1);
      else if (!path.startsWith("/")) path = `//${path}`;
    }
    return decodeURIComponent(path).replace(/\\/g, "/");
  }
  function reference(value = {}) {
    const ref = {};
    for (const key of ["path", "workspace_id", "run_id", "slot_id", "view", "fact_id", "revision", "result_key", "sha256", "version_id"])
      if (value[key] != null && value[key] !== "") ref[key] = value[key];
    if (["fact_id", "revision", "result_key", "sha256", "version_id"].some(key => ref[key] != null)) ref.view = "history";
    ref.view ||= "current";
    return ref;
  }
  function follow(event, { onFile, onExternal, onError }) {
    const anchor = event.target.closest("a[href]");
    if (!anchor || anchor.hasAttribute?.("download")) return;
    const href = anchor.getAttribute("href") || "";
    if (!href || href.startsWith("#") || /^blob:/i.test(href)) return;
    event.preventDefault(); event.stopPropagation();
    try {
      if (/^https?:\/\//i.test(href)) {
        if (!onExternal) throw new Error("当前运行环境无法打开外部链接");
        return Promise.resolve(onExternal(href)).catch(onError);
      }
      const path = pathFromHref(href);
      if (!path) throw new Error("不支持此文件链接");
      return Promise.resolve(onFile(path, anchor)).catch(onError);
    } catch (error) { onError(new Error(`文件链接无效: ${error.message}`)); }
  }
  return Object.freeze({ pathFromHref, reference, follow });
});
