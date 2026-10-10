/* 本文件对外提供 FocusSurfaceTransition 的表面显隐、原生 dialog 和保留节点的布局过渡。
 * 输入为 DOM 元素、最终可见意图或同步布局操作；输出为可取消的完成 Promise，不持有业务身份或状态。
 * 工作流为采样当前呈现、替换同一元素的动画，退出立即 inert、完成再 hidden/close；偏好和窗口变化收敛到最后意图。
 * 示例：FocusSurfaceTransition.visible(drawer, false, { axis: "x" }); layout(form, () => toggleDetailsDOM())。
 */
(function (root) {
  "use strict";
  const running = new WeakMap(), origins = new WeakMap(), active = new Set();
  const reduced = root.matchMedia("(prefers-reduced-motion: reduce), (update: slow)");
  function finish(element) { running.get(element)?.settle(true); }
  function replace(element, frames, done, timing = "--motion-standard") {
    running.get(element)?.settle(false);
    const style = root.getComputedStyle(element);
    const duration = Number.parseFloat(style.getPropertyValue(timing));
    if (reduced.matches || !element.isConnected || !Number.isFinite(duration) || duration <= 0) { done(); return Promise.resolve(true); }
    return new Promise(resolve => {
      const animation = element.animate(frames, { duration, easing: style.getPropertyValue("--ease-standard").trim(), fill: "both" });
      const entry = { settle(commit) {
        if (running.get(element) !== entry) return;
        running.delete(element); active.delete(element); animation.cancel();
        if (commit) done();
        resolve(commit);
      } };
      running.set(element, entry); active.add(element);
      animation.finished.then(() => entry.settle(true), () => entry.settle(false));
    });
  }
  function visible(element, show, { axis = "y", distance = 12, modal = false, source = null } = {}) {
    const wasHidden = modal ? !element.open : element.hidden;
    const style = !wasHidden ? root.getComputedStyle(element) : null;
    const from = { opacity: wasHidden ? 0 : style.opacity, transform: wasHidden ? `translate${axis.toUpperCase()}(${distance}px)` : style.transform };
    running.get(element)?.settle(false);
    element.dataset.surfaceManaged = "";
    if (show) {
      delete element.dataset.surfaceClosing;
      if (source?.isConnected) origins.set(element, source);
      if (modal && !element.open) element.showModal();
      element.hidden = false; element.inert = false;
      if (wasHidden && source?.isConnected && modal) {
        const origin = source.getBoundingClientRect(), target = element.getBoundingClientRect();
        from.transform = `translate(${Math.max(-32, Math.min(32, origin.x + origin.width / 2 - target.x - target.width / 2))}px, ${Math.max(-24, Math.min(24, origin.y + origin.height / 2 - target.y - target.height / 2))}px)`;
      }
    } else { element.inert = true; if (modal) element.dataset.surfaceClosing = ""; }
    if (wasHidden && !show) return Promise.resolve(true);
    let target = `translate${axis.toUpperCase()}(${distance}px)`;
    const origin = origins.get(element);
    if (!show && modal && origin?.isConnected) {
      const rect = origin.getBoundingClientRect(), current = element.getBoundingClientRect();
      target = `translate(${Math.max(-32, Math.min(32, rect.x + rect.width / 2 - current.x - current.width / 2))}px, ${Math.max(-24, Math.min(24, rect.y + rect.height / 2 - current.y - current.height / 2))}px)`;
    }
    return replace(element, [from, { opacity: show ? 1 : 0, transform: show ? "none" : target }], () => {
      if (!show) { if (modal && element.open) element.close(); else element.hidden = true; }
      if (!show) origins.delete(element);
      element.inert = !show;
      delete element.dataset.surfaceClosing;
    });
  }
  function layout(element, update) {
    const before = element.getBoundingClientRect();
    running.get(element)?.settle(false);
    update();
    const after = element.getBoundingClientRect();
    return replace(element, [
      { transform: `translate(${before.x - after.x}px, ${before.y - after.y}px)`, width: `${before.width}px`, height: `${before.height}px` },
      { transform: "none", width: `${after.width}px`, height: `${after.height}px` },
    ], () => {}, "--motion-deliberate");
  }
  function settleAll() { for (const element of [...active]) finish(element); }
  reduced.addEventListener("change", settleAll);
  root.addEventListener("resize", settleAll);
  root.FocusSurfaceTransition = Object.freeze({ visible, layout, finish });
})(globalThis);
