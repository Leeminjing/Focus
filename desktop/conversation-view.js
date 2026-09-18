/*
 * 本文件对外提供会话区的 DOM 写入侧：reconcile（对账写入会话内容）、syncStreaming/syncStreamingPlaceholder
 * （流式占位的新建与同步）、windowLimit/increaseWindow（长会话窗口边界）与 loadEarlier（加载更早内容并保持
 * 阅读位置）。输入为会话容器节点、目标 HTML、流式缓冲与窗口状态；输出为保持节点身份与阅读态的 DOM 更新，
 * 以及一次只触碰变化节点的流式写入。具体工作流为 configure 注入对账器、事件归一、渲染模块与 markdown；
 * reconcile 分两个阶段——只读的**计划**（归类容器子节点，交给对账器产出 keep/update/append/remove）与
 * 唯一改动 DOM 的**应用**（复用、原地更新、移入新节点、按目标顺序就位）；syncStreamingPlaceholder 按 run
 * 身份找到或就地创建占位并随即同步其内容（占位属性取自渲染模块的身份常量，保证与渲染侧单元逐字一致）；
 * loadEarlier 在提高窗口后按高度差回填滚动位置。
 * 子节点归类约定：带身份键者参与对账（`data-unit-key`，或消息/分界/流式的既有键；没有显式键的事件序列
 * 回退取首事件键）；声明保留者（`data-conversation-preserved` 或 PRESERVED_SELECTORS 命中，且自身没有
 * 身份键）不参与对账，应用阶段不触碰其位置与内容（排序锚点一律取非保留节点，整体重建的回落路径也把保留
 * 节点留回容器）；其余无键节点按"显式丢弃后重建"处理——不存在既不清算也不移除的中间态。事件节点的搬运只在
 * 决策完成后、对已判为 update 的单元进行，因此不会出现"活动节点被搬空后又被复用"的情形。交给对账器的单元
 * 沿用其契约字段（kind / key / signature；本模块始终给出 signature，kind 仅为契约完整性保留）。
 * 阅读态约定：展开态与按需挂载的详情体属于交互态，事件与事件序列的签名只取摘要行（剔除 `open` 与详情体），
 * 因此无关更新不触碰它们；自身内容变化时保留展开态并按当前记录重新挂载详情体。
 * 滚动约定：登记（`trackScroll`）发生在写入之前，因此首次建立基线时读到的是写入前的高度；贴底且内容增长时
 * 按高度差跟随；不在底部时按**阅读锚点**（写入前记录的、视口内最上面几条执行行的视口坐标）在写入后把滚动
 * 位置回正，使窗口上边界前移不打扰正在向上阅读的用户。锚点记录在 `rememberScroll`（用户滚动或显式定位）时
 * 作废，避免用过期基点回正。
 * 示例：`configure({ reconciler, events, render, markdown, eventIndex }); reconcile(conversation, html)`。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusConversationView = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const PRESERVED_ATTRIBUTE = "data-conversation-preserved";
  const PRESERVED_SELECTORS = [".trace-panel", ".review-panel", ".access-review-panel"];
  const EVENT_SELECTOR = ".conversation-event[data-event-key]";
  const SEQUENCE_CLASS = "conversation-event-sequence";
  const BOTTOM_THRESHOLD = 80;
  const READING_ANCHORS = 3;

  let deps = null;
  const windows = new Map();
  const lazyDetailHosts = new WeakSet();
  const scrollHosts = new WeakSet();
  const scrollState = new WeakMap();
  const pendingAnchors = new WeakMap();

  function configure(next) {
    deps = next;
  }

  function windowLimit(taskId) {
    return windows.get(taskId) || (deps?.render?.WINDOW_MESSAGES ?? 80);
  }

  function increaseWindow(taskId) {
    const step = deps?.render?.WINDOW_MESSAGES ?? 80;
    const next = windowLimit(taskId) + step;
    windows.set(taskId, next);
    return next;
  }

  function loadEarlier(conversation, taskId, reconcileFn) {
    if (!conversation) return;
    const previousHeight = conversation.scrollHeight || 0;
    const previousTop = conversation.scrollTop || 0;
    increaseWindow(taskId);
    if (typeof reconcileFn === "function") reconcileFn();
    conversation.scrollTop = previousTop + Math.max(0, (conversation.scrollHeight || 0) - previousHeight);
    rememberScroll(conversation);
  }

  // === 滚动锚点 ===

  function _atBottom(conversation) {
    return (conversation.scrollHeight || 0) - (conversation.scrollTop || 0) - (conversation.clientHeight || 0) < BOTTOM_THRESHOLD;
  }

  function rememberScroll(conversation) {
    if (!conversation) return;
    pendingAnchors.delete(conversation);
    scrollState.set(conversation, {
      height: conversation.scrollHeight || 0,
      pinned: _atBottom(conversation),
    });
  }

  function _registerScrollHost(conversation) {
    if (!conversation || typeof conversation.addEventListener !== "function") return;
    if (scrollHosts.has(conversation)) return;
    scrollHosts.add(conversation);
    conversation.addEventListener("scroll", () => rememberScroll(conversation), { passive: true });
  }

  // 写入前登记：注册滚动监听，并在尚未记录过基线时补一次引导读取（未走 renderFocus 的容器）。
  // 顺序有意义——引导读取必须发生在写入之前，否则读到的是写入后的高度，
  // 会把"贴底跟随"误判为"用户正在向上阅读"。稳态下基线由上一次写入与滚动事件维护，本函数不再读取。
  function trackScroll(conversation) {
    _registerScrollHost(conversation);
    if (conversation && !scrollState.has(conversation)) rememberScroll(conversation);
  }

  function scrollStateOf(conversation) {
    const state = scrollState.get(conversation);
    return state ? { ...state } : null;
  }

  // 阅读锚点：不在底部时记下视口内最上面的几条执行行（没有可见行时取顶层单元），
  // 写入后按同一节点的位移把滚动位置原样回正。不按"被移除节点的高度"计量：等高替换（"加载更早"入口的
  // 计数变化）、行间距、上方内容变高都会让那个假设失真；锚点位移由浏览器算出，与内容如何变化无关。
  // 记录发生在只读的计划阶段之后、任何 DOM 改动之前。
  function noteReadingAnchor(host, units) {
    if (!host || !units?.length) return null;
    const state = scrollState.get(host);
    if (!state || state.pinned) return null;
    const viewportTop = host.getBoundingClientRect?.().top;
    if (viewportTop === undefined) return null;
    const candidates = [
      ...[...(host.querySelectorAll?.(EVENT_SELECTOR) || [])].map(node => ({ kind: "row", key: node.dataset.eventKey, node })),
      ...units.map(node => ({ kind: "unit", key: _explicitKey(node) || _sequenceKey(node), node })),
    ];
    const anchors = [];
    for (const candidate of candidates) {
      const box = candidate.node.getBoundingClientRect?.();
      if (!box || box.bottom <= viewportTop) continue;
      anchors.push({ kind: candidate.kind, key: candidate.key, top: box.top });
      if (anchors.length >= READING_ANCHORS) break;
    }
    if (!anchors.length) return null;
    pendingAnchors.set(host, anchors);
    return anchors;
  }

  function _anchorNode(host, anchor) {
    if (anchor.kind === "row") {
      for (const node of host.querySelectorAll?.(EVENT_SELECTOR) || []) {
        if (node.dataset.eventKey === anchor.key) return node;
      }
      return null;
    }
    for (const node of host.children || []) {
      if ((_explicitKey(node) || _sequenceKey(node)) === anchor.key) return node;
    }
    return null;
  }

  function _syncReadingAnchor(host) {
    const anchors = pendingAnchors.get(host);
    pendingAnchors.delete(host);
    if (!anchors) return 0;
    for (const anchor of anchors) {
      const node = _anchorNode(host, anchor);
      if (!node) continue;
      const top = node.getBoundingClientRect?.().top;
      if (top === undefined) return 0;
      const shift = top - anchor.top;
      if (!shift) return 0;
      host.scrollTop = Math.max(0, (host.scrollTop || 0) + shift);
      return shift;
    }
    return 0;
  }

  // 写入后的滚动跟随：一次布局读取与至多一次滚动写入。
  // 用户在底部且本次是内容增长才跟随（按高度差回填）；不在底部时按阅读锚点回正，使阅读位置落在同一行。
  // 净高度减少不跟随时由浏览器自身的 clamp 收口，因此不会额外产生一次由本模块发起的位移。
  function syncScrollAfterWrite(conversation) {
    if (!conversation) return 0;
    _registerScrollHost(conversation);
    const height = conversation.scrollHeight || 0;
    const previous = scrollState.get(conversation) || { height, pinned: true };
    const grew = height > previous.height;
    let delta = 0;
    if (previous.pinned && grew) {
      delta = height - previous.height;
      conversation.scrollTop = height;
    } else if (!previous.pinned) {
      delta = _syncReadingAnchor(conversation);
    }
    scrollState.set(conversation, { height, pinned: previous.pinned });
    return delta;
  }

  function _explicitKey(node) {
    const dataset = node?.dataset;
    if (!dataset) return null;
    if (dataset.unitKey) return dataset.unitKey;
    if (dataset.streamRun !== undefined) return `stream:${dataset.streamRun}`;
    if (dataset.messageKey !== undefined) return dataset.messageKey;
    if (dataset.dividerKey !== undefined) return dataset.dividerKey;
    return null;
  }

  function _sequenceKey(node) {
    if (!node?.classList?.contains(SEQUENCE_CLASS)) return null;
    const first = node.querySelector?.(EVENT_SELECTOR);
    return first ? `seq:${first.dataset.eventKey}` : null;
  }

  function _isPreserved(node) {
    if (!node || node.nodeType !== 1) return false;
    if (_explicitKey(node) !== null) return false;
    if (typeof node.hasAttribute === "function" && node.hasAttribute(PRESERVED_ATTRIBUTE)) return true;
    return PRESERVED_SELECTORS.some(selector => typeof node.matches === "function" && node.matches(selector));
  }

  function _summaryOf(event) {
    return event.querySelector?.(":scope > summary") || event.firstElementChild || null;
  }

  function _eventSignature(event) {
    const summary = _summaryOf(event);
    return String(summary?.outerHTML || event.outerHTML || "").replace(/\s+open(="[^"]*")?/g, "");
  }

  function _sequenceSignature(node) {
    const body = [...(node.children || [])].map(_eventSignature).join("");
    const attributes = [...(node.attributes || [])]
      .filter(attribute => attribute.name !== "open")
      .map(attribute => `${attribute.name}=${attribute.value}`)
      .join(" ");
    return `${node.className}|${attributes}|${body}`;
  }

  function _signatureOf(node) {
    return node?.classList?.contains(SEQUENCE_CLASS) ? _sequenceSignature(node) : (node.outerHTML || "");
  }

  function _unitFromNode(node) {
    const key = _explicitKey(node) || _sequenceKey(node);
    return { kind: key === null ? "unkeyed" : "keyed", key, signature: _signatureOf(node) };
  }

  function _eventNodes(root) {
    return [...(root.querySelectorAll?.(EVENT_SELECTOR) || [])];
  }

  function _eventNodeMap(root) {
    const found = new Map();
    const ambiguous = new Set();
    for (const event of _eventNodes(root)) {
      const key = event.dataset.eventKey;
      if (found.has(key)) ambiguous.add(key);
      else found.set(key, event);
    }
    for (const key of ambiguous) found.delete(key);
    return found;
  }

  function _openStates(nodes) {
    const states = new Map();
    for (const node of nodes) {
      for (const event of _eventNodes(node)) states.set(event.dataset.eventKey, event.open);
    }
    return states;
  }

  function _plan(container, target) {
    const prevNodes = [...(container.children || [])].filter(node => !_isPreserved(node));
    const nextNodes = [...(target.children || [])].filter(node => !_isPreserved(node));
    return {
      prevNodes,
      nextNodes,
      prevUnits: prevNodes.map(_unitFromNode),
      nextUnits: nextNodes.map(_unitFromNode),
    };
  }

  function _syncElementAttributes(current, next) {
    for (const attribute of [...current.attributes]) {
      if (!next.hasAttribute(attribute.name)) current.removeAttribute(attribute.name);
    }
    for (const attribute of [...next.attributes]) current.setAttribute(attribute.name, attribute.value);
  }

  function _fillDetail(event, body) {
    if (!body) return;
    const record = deps?.eventIndex?.get(event.dataset.eventKey);
    if (!record) return;
    body.innerHTML = deps.events.renderEventDetail(record);
    body.dataset.detailLazy = "0";
  }

  function _syncEventElement(current, next) {
    if (_eventSignature(current) === _eventSignature(next)) return;
    const wasOpen = current.open;
    _syncElementAttributes(current, next);
    const currentSummary = _summaryOf(current);
    const nextSummary = _summaryOf(next);
    if (currentSummary && nextSummary && currentSummary !== nextSummary) currentSummary.replaceWith(nextSummary);
    current.open = wasOpen;
    if (wasOpen) _fillDetail(current, current.querySelector?.(".conversation-event-detail"));
  }

  function _rowUnit(node) {
    return { kind: "row", key: node.dataset.eventKey, signature: _eventSignature(node) };
  }

  function _applyRowPatches(container, patches, plan, removals) {
    const { prevRows, nextRows } = plan;
    const nextToNode = new Array(nextRows.length).fill(null);
    const keptPrev = new Set();
    for (const patch of patches) {
      if (patch.op === "keep") {
        nextToNode[patch.nextIndex] = prevRows[patch.prevIndex];
        keptPrev.add(patch.prevIndex);
        continue;
      }
      if (patch.op === "update") {
        _syncEventElement(prevRows[patch.prevIndex], nextRows[patch.nextIndex]);
        nextToNode[patch.nextIndex] = prevRows[patch.prevIndex];
        keptPrev.add(patch.prevIndex);
        continue;
      }
      if (patch.op === "append") nextToNode[patch.nextIndex] = nextRows[patch.nextIndex];
    }
    for (let index = 0; index < nextToNode.length; index += 1) {
      if (nextToNode[index] == null) nextToNode[index] = nextRows[index];
    }
    for (let index = 0; index < prevRows.length; index += 1) {
      if (!keptPrev.has(index)) removals.push(prevRows[index]);
    }
    let anchor = null;
    for (const node of nextToNode) {
      if (anchor === null) {
        if (container.firstChild !== node) container.insertBefore(node, container.firstChild);
      } else if (anchor.nextSibling !== node) {
        container.insertBefore(node, anchor.nextSibling);
      }
      anchor = node;
    }
  }

  function _uniqueKeys(nodes) {
    const keys = new Set();
    for (const node of nodes) {
      const key = node.dataset.eventKey;
      if (keys.has(key)) return false;
      keys.add(key);
    }
    return true;
  }

  function _reconcileEventSequence(current, next, removals) {
    if (!current.matches?.(`.${SEQUENCE_CLASS}`) || !next.matches?.(`.${SEQUENCE_CLASS}`)) return false;
    const prevRows = [...current.children];
    const nextRows = [...next.children];
    if ([...prevRows, ...nextRows].some(node => !node.matches?.(EVENT_SELECTOR))) return false;
    if (!_uniqueKeys(prevRows) || !_uniqueKeys(nextRows)) return false;
    _syncElementAttributes(current, next);
    const patches = deps.reconciler.diffUnits(prevRows.map(_rowUnit), nextRows.map(_rowUnit));
    _applyRowPatches(current, patches, { prevRows, nextRows }, removals);
    return true;
  }

  function _transplantEvents(current, incoming) {
    const currentByKey = _eventNodeMap(current);
    for (const nextEvent of _eventNodes(incoming)) {
      const currentEvent = currentByKey.get(nextEvent.dataset.eventKey);
      if (!currentEvent || currentEvent === nextEvent) continue;
      _syncEventElement(currentEvent, nextEvent);
      nextEvent.replaceWith(currentEvent);
    }
  }

  function _updateUnit(current, incoming, openStates, removals) {
    if (_reconcileEventSequence(current, incoming, removals)) return current;
    _transplantEvents(current, incoming);
    for (const event of _eventNodes(incoming)) {
      if (openStates.has(event.dataset.eventKey)) event.open = openStates.get(event.dataset.eventKey);
    }
    return incoming;
  }

  function _firstDiffable(container) {
    for (const child of container.children || []) if (!_isPreserved(child)) return child;
    return null;
  }

  function _nextDiffable(node) {
    let sibling = node.nextSibling;
    while (sibling && _isPreserved(sibling)) sibling = sibling.nextSibling;
    return sibling;
  }

  function _order(conversation, orderedNodes) {
    let anchor = null;
    for (const node of orderedNodes) {
      const reference = anchor === null ? _firstDiffable(conversation) : _nextDiffable(anchor);
      if (reference !== node) conversation.insertBefore(node, reference);
      anchor = node;
    }
  }

  // 回收被淘汰的节点。阅读位置的补偿由写入侧按锚点位移完成，与这里的移除动作无关。
  function _discardCollected(removals) {
    if (!removals.length) return;
    for (const node of removals) node.remove();
    removals.length = 0;
  }

  function _apply(conversation, patches, plan, removals) {
    const { prevNodes, nextNodes } = plan;
    const openStates = _openStates(prevNodes);
    const nextToNode = new Array(nextNodes.length).fill(null);
    const keptPrev = new Set();
    for (const patch of patches) {
      if (patch.op === "keep") {
        nextToNode[patch.nextIndex] = prevNodes[patch.prevIndex];
        keptPrev.add(patch.prevIndex);
        continue;
      }
      if (patch.op === "update") {
        const current = prevNodes[patch.prevIndex];
        const resolved = _updateUnit(current, nextNodes[patch.nextIndex], openStates, removals);
        if (resolved === current) keptPrev.add(patch.prevIndex);
        nextToNode[patch.nextIndex] = resolved;
        continue;
      }
      if (patch.op === "append") nextToNode[patch.nextIndex] = nextNodes[patch.nextIndex];
    }
    for (let index = 0; index < nextToNode.length; index += 1) {
      if (nextToNode[index] == null) nextToNode[index] = nextNodes[index];
    }
    for (let index = 0; index < prevNodes.length; index += 1) {
      if (!keptPrev.has(index)) removals.push(prevNodes[index]);
    }
    _discardCollected(removals);
    _order(conversation, nextToNode);
  }

  function mountLazyDetails(conversation) {
    if (!conversation || typeof conversation.addEventListener !== "function") return;
    if (lazyDetailHosts.has(conversation)) return;
    lazyDetailHosts.add(conversation);
    conversation.addEventListener("toggle", event => {
      const node = event.target;
      if (!node?.matches?.("details.conversation-event") || !node.open) return;
      _fillDetail(node, node.querySelector?.(".conversation-event-detail[data-detail-lazy='1']"));
    }, true);
  }

  function _rebuild(conversation, html) {
    const preserved = [...(conversation.children || [])].filter(_isPreserved);
    conversation.innerHTML = html;
    for (const node of preserved) conversation.append(node);
    mountLazyDetails(conversation);
  }

  function reconcile(conversation, html) {
    const template = document.createElement?.("template");
    if (!template?.content || !conversation.replaceChildren || !deps?.reconciler) {
      _rebuild(conversation, html);
      return;
    }
    template.innerHTML = html;
    const plan = _plan(conversation, template.content);
    let patches;
    try {
      patches = deps.reconciler.diffUnits(plan.prevUnits, plan.nextUnits);
    } catch (error) {
      _rebuild(conversation, html);
      return;
    }
    noteReadingAnchor(conversation, plan.prevNodes);
    _apply(conversation, patches, plan, []);
    mountLazyDetails(conversation);
  }

  function syncBlocks(container, blocks, rendered) {
    for (let index = 0; index < blocks.length; index += 1) {
      if (rendered[index] === blocks[index]) continue;
      const template = document.createElement("template");
      template.innerHTML = blocks[index];
      const node = template.content.firstElementChild;
      const existing = container.children[index];
      if (existing) existing.replaceWith(node);
      else container.append(node);
    }
    while (container.children.length > blocks.length) {
      container.lastElementChild.remove();
    }
    return blocks.slice();
  }

  function syncStreamingPlaceholder(conversation, runId, buffer) {
    const placeholderClass = deps?.render?.STREAMING_PLACEHOLDER_CLASS || "work-record message ai streaming";
    let article = conversation.querySelector?.(`[data-stream-run="${runId}"]`);
    if (!article) {
      article = document.createElement("article");
      article.className = placeholderClass;
      article.dataset.streamRun = runId;
      conversation.append(article);
    }
    syncStreaming(article, buffer);
    return article;
  }

  function syncStreaming(article, buffer) {
    const header = deps?.render?.STREAMING_HEADER_HTML || "";
    if (!article.firstElementChild) article.insertAdjacentHTML("afterbegin", header);
    const reasoningHtml = buffer.reasoning
      ? `<section class="conversation-event-sequence" role="group" aria-label="执行过程">${deps.events.renderEvent({ type: "reasoning", content: buffer.reasoning }, { previewMode: "latest" })}</section>`
      : "";
    if (buffer.reasoningRendered !== reasoningHtml) {
      const reasoning = article.querySelector(":scope > .conversation-event-sequence");
      if (reasoningHtml) {
        const template = document.createElement("template");
        template.innerHTML = reasoningHtml;
        const node = template.content.firstElementChild;
        if (reasoning) reasoning.replaceWith(node);
        else article.insertBefore(node, article.querySelector(":scope > .message-rich"));
      } else if (reasoning) {
        reasoning.remove();
      }
      buffer.reasoningRendered = reasoningHtml;
    }

    let body = article.querySelector(":scope > .message-rich");
    if (!buffer.text) {
      if (body) body.remove();
      buffer.blocks = [];
      buffer.blockEntries = [];
      return;
    }
    if (!body) {
      body = document.createElement("div");
      body.className = "message-rich";
      article.append(body);
    }
    const blocks = deps.render.streamingBlocks(deps.markdown, buffer).map(entry => entry.html);
    buffer.blocks = syncBlocks(body, blocks, buffer.blocks || []);
  }

  return {
    PRESERVED_ATTRIBUTE,
    PRESERVED_SELECTORS,
    configure,
    reconcile,
    mountLazyDetails,
    syncStreaming,
    syncStreamingPlaceholder,
    syncBlocks,
    trackScroll,
    rememberScroll,
    scrollStateOf,
    syncScrollAfterWrite,
    windowLimit,
    increaseWindow,
    loadEarlier,
  };
});
