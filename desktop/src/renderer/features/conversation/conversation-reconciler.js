/*
 * 本文件对外提供对话区 DOM 增量对账的纯函数工具，不持有应用状态、不直接操作 DOM。
 * 输入为带稳定 key 的渲染单元序列（消息/事件/分界），输出为按 next 顺序排列的编辑
 * 指令（keep / update / append / remove）；具体工作流为：以 next 为主序，按 key 在 prev
 * 中定位同身份单元——同 key 且签名一致记为 keep（保留原 DOM 节点），同 key 签名不同记为
 * update（原位更新），next 中未命中记为 append，prev 中未命中或无 key 记为 remove。
 * 示例：`diffUnits(prevUnits, nextUnits)` 返回 `[{op:"keep",...},{op:"append",...}]`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusConversationReconciler = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  // 单元结构: { kind: string, key: string|null, signature?: string }
  // key 为对账身份；同 key 且签名一致 → keep，同 key 签名不同 → update。

  function unitKey(unit) {
    return unit && typeof unit.key === "string" && unit.key ? unit.key : null;
  }

  function _signature(unit) {
    if (unit && unit.signature != null) return String(unit.signature);
    if (!unit) return "";
    return `${unit.kind}:${unit.key}`;
  }

  function diffUnits(prev, next) {
    const p = Array.isArray(prev) ? prev : [];
    const n = Array.isArray(next) ? next : [];

    const patches = [];

    // prev 中有 key 的单元按序排队，供 next 按序匹配。
    const prevKeyedQueue = [];
    for (let i = 0; i < p.length; i++) {
      const key = unitKey(p[i]);
      if (key !== null) prevKeyedQueue.push({ index: i, key });
    }

    let cursor = 0;
    const usedPrev = new Set();

    for (let j = 0; j < n.length; j++) {
      const nKey = unitKey(n[j]);
      if (nKey === null) {
        patches.push({ op: "append", nextIndex: j });
        continue;
      }
      let matched = -1;
      for (let k = cursor; k < prevKeyedQueue.length; k++) {
        if (prevKeyedQueue[k].key === nKey) { matched = k; break; }
      }
      if (matched === -1) {
        patches.push({ op: "append", nextIndex: j });
        continue;
      }
      const entry = prevKeyedQueue[matched];
      usedPrev.add(entry.index);
      patches.push({
        op: _signature(p[entry.index]) === _signature(n[j]) ? "keep" : "update",
        prevIndex: entry.index,
        nextIndex: j,
      });
      cursor = matched + 1;
    }

    // prev 中未命中或 key 为空的单元 → remove。
    for (let i = 0; i < p.length; i++) {
      if (unitKey(p[i]) === null || !usedPrev.has(i)) {
        patches.push({ op: "remove", prevIndex: i });
      }
    }

    // 非 remove 指令按 next 顺序排列，remove 排在末尾（remove 不参与最终顺序重建）。
    patches.sort((a, b) => {
      if (a.op === "remove" && b.op === "remove") return 0;
      if (a.op === "remove") return 1;
      if (b.op === "remove") return -1;
      return a.nextIndex - b.nextIndex;
    });

    return patches;
  }

  return { unitKey, diffUnits };
});
