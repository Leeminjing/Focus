/* Shared keyboard semantics for resizable separators and automatically activated tabs.
 * Callers own layout, persistence and focus restoration; ranges are read from real layout.
 */
(function (root) {
  "use strict";
  function syncSeparator(node, range) {
    if (!node || !range) return;
    for (const name of ["min", "max", "now"]) {
      node.setAttribute(`aria-value${name}`, String(Math.round(range[name])));
    }
  }
  function bindSeparator(node, { read, resize, commit = () => {}, step = 24, largeStep = 64, direction = 1, signal }) {
    node.tabIndex = 0;
    const sync = () => syncSeparator(node, read());
    node.addEventListener("keydown", event => {
      if (event.altKey || event.ctrlKey || event.metaKey || event.defaultPrevented) return;
      const horizontal = node.getAttribute("aria-orientation") === "horizontal";
      const backward = horizontal ? "ArrowUp" : "ArrowLeft", forward = horizontal ? "ArrowDown" : "ArrowRight";
      if (![backward, forward, "Home", "End"].includes(event.key)) return;
      const range = read();
      if (!range) return;
      event.preventDefault();
      const delta = (event.key === backward ? -1 : 1) * direction * (event.shiftKey ? largeStep : step);
      const value = event.key === "Home" ? range.min : event.key === "End" ? range.max : range.now + delta;
      resize(Math.max(range.min, Math.min(range.max, value)));
      sync();
      commit();
    }, signal ? { signal } : undefined);
    sync();
    return sync;
  }
  function tabDestination(event) {
    if (event.altKey || event.ctrlKey || event.metaKey || event.defaultPrevented) return null;
    const tab = event.target.closest?.('[role="tab"]'), list = tab?.closest('[role="tablist"]');
    if (!list) return null;
    const vertical = list.getAttribute("aria-orientation") === "vertical";
    const backward = vertical ? "ArrowUp" : "ArrowLeft", forward = vertical ? "ArrowDown" : "ArrowRight";
    if (![backward, forward, "Home", "End"].includes(event.key)) return null;
    const tabs = [...list.querySelectorAll('[role="tab"]')].filter(item => !item.disabled && !item.hidden);
    const index = tabs.indexOf(tab);
    if (index < 0) return null;
    event.preventDefault();
    return tabs[event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === forward ? 1 : -1) + tabs.length) % tabs.length];
  }
  function announce(node, text) {
    if (node && node.textContent !== text) node.textContent = text;
  }
  root.FocusAccessibleControls = Object.freeze({ syncSeparator, bindSeparator, tabDestination, announce });
})(globalThis);
