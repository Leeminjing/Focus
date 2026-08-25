/*
 * 本文件对外提供 FocusPatrolAvatar 会话小兵组件。输入为挂载节点、待命/真实 Patrol
 * 视图模型、任务级归一化位置与动作/保存回调，输出为可访问的状态化小兵、气泡和自由
 * 拖动交互；工作流为按 avatar_id 协调 DOM，使用 Pointer Events/键盘更新受限坐标，以
 * 稳定方向采样独立更新动作资源，并仅在交互结束时把位置或主操作交还宿主。示例：
 * FocusPatrolAvatar.mount(root, options)。
 */
(function patrolAvatarModule(global) {
  "use strict";

  const ASSET_ROOT = "./assets/patrol-avatar";
  const DRAG_THRESHOLD = 5;
  const MOTION_SAMPLE_DISTANCE = 12;
  const ROTATE_ENTER_RATIO = 1.6;
  const ROTATE_EXIT_RATIO = 2.2;
  const DEFAULT_SIZE = 116;
  const STATUS = Object.freeze({
    ready: { asset: "idle", label: "就绪", message: "我在这里，需要时可以继续安排任务。" },
    pending: { asset: "loading", label: "排队中", message: "任务已收到，正在等待开始。" },
    running: { asset: "working", label: "工作中", message: "正在执行当前 Patrol 任务。" },
    success: { asset: "happy", label: "已完成", message: "任务完成了，可以查看结果或继续安排。" },
    interrupted: { asset: "thinking", label: "已中断", message: "运行已中断，可以查看详情或继续。" },
    cancelled: { asset: "thinking", label: "已取消", message: "运行已取消，可以重新安排。" },
    error: { asset: "alert", label: "运行失败", message: "这次运行遇到问题，可以查看详情或重试。" },
  });

  function clamp(value, minimum = 0, maximum = 1) {
    return Math.min(maximum, Math.max(minimum, value));
  }

  function normalizeStatus(value) {
    return Object.hasOwn(STATUS, value) ? value : "ready";
  }

  function statusPresentation(value) {
    return STATUS[normalizeStatus(value)];
  }

  function sanitizePosition(value, fallback = { x: 0.16, y: 0.66 }) {
    const x = Number(value?.x);
    const y = Number(value?.y);
    return {
      x: clamp(Number.isFinite(x) ? x : fallback.x),
      y: clamp(Number.isFinite(y) ? y : fallback.y),
    };
  }

  function defaultPosition(index = 0) {
    const column = Math.max(0, index) % 4;
    const row = Math.floor(Math.max(0, index) / 4);
    return sanitizePosition({ x: 0.1 + column * 0.18, y: 0.7 - row * 0.18 });
  }

  function pixelPosition(position, bounds, size) {
    const safe = sanitizePosition(position);
    const maximumX = Math.max(0, Number(bounds?.width || 0) - Number(size?.width || 0));
    const maximumY = Math.max(0, Number(bounds?.height || 0) - Number(size?.height || 0));
    return { left: safe.x * maximumX, top: safe.y * maximumY };
  }

  function normalizedPosition(left, top, bounds, size) {
    const maximumX = Math.max(0, Number(bounds?.width || 0) - Number(size?.width || 0));
    const maximumY = Math.max(0, Number(bounds?.height || 0) - Number(size?.height || 0));
    return sanitizePosition({
      x: maximumX ? Number(left || 0) / maximumX : 0,
      y: maximumY ? Number(top || 0) / maximumY : 0,
    });
  }

  function dragExceeded(startX, startY, currentX, currentY, threshold = DRAG_THRESHOLD) {
    return Math.hypot(currentX - startX, currentY - startY) >= threshold;
  }

  function movementState(deltaX, deltaY, currentMotion = null) {
    const horizontal = Math.abs(deltaX);
    const vertical = Math.abs(deltaY);
    if (horizontal < 1 && vertical < 1) return null;
    const ratio = Math.min(horizontal, vertical) >= 1 ? Math.max(horizontal, vertical) / Math.min(horizontal, vertical) : Infinity;
    if (ratio <= (currentMotion === "rotate" ? ROTATE_EXIT_RATIO : ROTATE_ENTER_RATIO)) return "rotate";
    if (horizontal >= vertical) return deltaX < 0 ? "move-left" : "move-right";
    return deltaY < 0 ? "rise" : "descend";
  }

  function stableMovementState(sampleX, sampleY, currentX, currentY, currentMotion, threshold = MOTION_SAMPLE_DISTANCE) {
    const deltaX = currentX - sampleX;
    const deltaY = currentY - sampleY;
    if (Math.hypot(deltaX, deltaY) < threshold) return { motion: currentMotion, sampled: false };
    return { motion: movementState(deltaX, deltaY, currentMotion) || currentMotion, sampled: true };
  }

  function avatarId(avatar) {
    return String(avatar?.avatar_id || avatar?.agent_id || "");
  }

  function avatarStatus(avatar) {
    return avatar?.status || avatar?.latest_run?.status || "ready";
  }

  function avatarLabel(avatar) {
    if (avatar?.label) return avatar.label;
    return avatar?.agent_id ? `小兵 ${avatar.agent_id.slice(0, 8)}` : "Patrol 小兵";
  }

  function mount(root, initial = {}) {
    if (!root) return { update() {}, destroy() {} };
    let options = { avatars: null, agents: [], positions: {}, onPositionCommit() {}, onAction: null, onOpenDetails() {}, ...initial };
    let records = new Map();
    let resizeFrame = null;
    let zIndex = 1;
    let destroyed = false;

    function layerBounds() {
      return { width: root.clientWidth, height: root.clientHeight };
    }

    function recordSize(record) {
      const rectangle = record.element.getBoundingClientRect();
      return { width: rectangle.width || DEFAULT_SIZE, height: rectangle.height || DEFAULT_SIZE };
    }

    function applyPixelPosition(record, left, top) {
      const bounds = layerBounds();
      const size = recordSize(record);
      const maximumX = Math.max(0, bounds.width - size.width);
      const maximumY = Math.max(0, bounds.height - size.height);
      const safeLeft = clamp(Number(left) || 0, 0, maximumX);
      const safeTop = clamp(Number(top) || 0, 0, maximumY);
      record.left = safeLeft;
      record.top = safeTop;
      record.element.style.transform = `translate3d(${Math.round(safeLeft)}px, ${Math.round(safeTop)}px, 0)`;
      const x = maximumX ? safeLeft / maximumX : 0;
      record.element.dataset.bubbleAlign = x < 0.22 ? "start" : x > 0.78 ? "end" : "center";
    }

    function applyNormalizedPosition(record, position) {
      record.position = sanitizePosition(position, record.position || defaultPosition(record.index));
      const pixels = pixelPosition(record.position, layerBounds(), recordSize(record));
      applyPixelPosition(record, pixels.left, pixels.top);
    }

    function visualState(record) {
      if (record.motion) return record.motion;
      if (record.celebrating) return "happy";
      return statusPresentation(record.status).asset;
    }

    function applyVisualAsset(record) {
      const asset = visualState(record);
      if (record.visualAsset === asset) return false;
      record.visualAsset = asset;
      record.element.dataset.visual = asset;
      record.image.src = `${ASSET_ROOT}/${asset}.png`;
      return true;
    }

    function refreshVisual(record) {
      const presentation = statusPresentation(record.status);
      const statusLabel = record.avatar.status_label || presentation.label;
      record.element.dataset.status = record.status;
      applyVisualAsset(record);
      record.button.setAttribute("aria-label", `${avatarLabel(record.avatar)}，${statusLabel}。点击查看状态，方向键移动位置。`);
      record.statusNode.textContent = statusLabel;
      record.message.textContent = record.avatar.message || presentation.message;
    }

    function closeBubble(record) {
      record.bubble.hidden = true;
      record.button.setAttribute("aria-expanded", "false");
    }

    function toggleBubble(record) {
      const willOpen = record.bubble.hidden;
      records.forEach(item => closeBubble(item));
      if (!willOpen) return;
      record.bubble.hidden = false;
      record.button.setAttribute("aria-expanded", "true");
      record.element.style.zIndex = String(++zIndex);
    }

    function applyStatus(record, status) {
      const nextStatus = normalizeStatus(status);
      if (record.status === nextStatus) return refreshVisual(record);
      record.status = nextStatus;
      clearTimeout(record.celebrationTimer);
      record.celebrating = nextStatus === "success";
      if (record.celebrating) {
        record.celebrationTimer = setTimeout(() => {
          record.celebrating = false;
          refreshVisual(record);
        }, 2200);
      }
      refreshVisual(record);
    }

    function finishPointer(record, event, commit) {
      const drag = record.drag;
      if (!drag || event.pointerId !== drag.pointerId) return;
      record.drag = null;
      try {
        if (record.button.hasPointerCapture?.(event.pointerId)) record.button.releasePointerCapture(event.pointerId);
      } catch {}
      record.motion = null;
      record.element.classList.remove("is-dragging");
      if (!drag.started || !commit) {
        applyNormalizedPosition(record, record.position);
        refreshVisual(record);
        return;
      }
      record.suppressClick = true;
      record.position = normalizedPosition(record.left, record.top, layerBounds(), recordSize(record));
      applyNormalizedPosition(record, record.position);
      refreshVisual(record);
      options.onPositionCommit(record.avatarId, { ...record.position });
      setTimeout(() => { record.suppressClick = false; }, 0);
    }

    function createRecord(avatar, index, position) {
      const id = avatarId(avatar);
      const element = document.createElement("article");
      const button = document.createElement("button");
      const image = document.createElement("img");
      const fallback = document.createElement("span");
      const bubble = document.createElement("section");
      const heading = document.createElement("strong");
      const statusNode = document.createElement("span");
      const message = document.createElement("p");
      const detail = document.createElement("button");
      const bubbleId = `patrol-avatar-bubble-${id}`;

      element.className = "patrol-avatar";
      element.dataset.avatarId = id;
      if (avatar.agent_id) element.dataset.agentId = avatar.agent_id;
      element.dataset.presence = avatar.presence || "agent";
      button.type = "button";
      button.className = "patrol-avatar__button";
      button.setAttribute("aria-controls", bubbleId);
      button.setAttribute("aria-expanded", "false");
      image.className = "patrol-avatar__image";
      image.alt = "";
      image.draggable = false;
      fallback.className = "patrol-avatar__fallback";
      fallback.textContent = "PATROL";
      fallback.hidden = true;
      bubble.id = bubbleId;
      bubble.className = "patrol-avatar__bubble";
      bubble.hidden = true;
      heading.textContent = avatarLabel(avatar);
      statusNode.className = "patrol-avatar__status";
      message.className = "patrol-avatar__message";
      detail.type = "button";
      detail.className = "patrol-avatar__detail";
      detail.textContent = avatar.action_label || "查看详情";
      button.append(image, fallback);
      bubble.append(heading, statusNode, message, detail);
      element.append(button, bubble);
      root.append(element);

      const record = {
        avatar, avatarId: id, index, element, button, image, fallback, bubble, heading, detail, statusNode, message,
        position: sanitizePosition(position, defaultPosition(index)),
        status: "ready", motion: null, visualAsset: null, drag: null, suppressClick: false,
        celebrating: false, celebrationTimer: null, left: 0, top: 0,
      };

      image.addEventListener("error", () => {
        image.hidden = true;
        fallback.hidden = false;
      });
      image.addEventListener("load", () => {
        image.hidden = false;
        fallback.hidden = true;
      });
      button.addEventListener("click", event => {
        if (record.suppressClick) {
          event.preventDefault();
          return;
        }
        toggleBubble(record);
      });
      detail.addEventListener("click", event => {
        event.stopPropagation();
        if (typeof options.onAction === "function") options.onAction(record.avatar);
        else if (record.avatar.agent_id) options.onOpenDetails(record.avatar.agent_id);
      });
      button.addEventListener("pointerdown", event => {
        if (event.button !== 0 || event.isPrimary === false) return;
        const rootRectangle = root.getBoundingClientRect();
        const rectangle = element.getBoundingClientRect();
        record.element.style.zIndex = String(++zIndex);
        record.drag = {
          pointerId: event.pointerId,
          startX: event.clientX,
          startY: event.clientY,
          lastX: event.clientX,
          lastY: event.clientY,
          startLeft: rectangle.left - rootRectangle.left,
          startTop: rectangle.top - rootRectangle.top,
          started: false,
        };
        try { button.setPointerCapture(event.pointerId); } catch {}
      });
      button.addEventListener("pointermove", event => {
        const drag = record.drag;
        if (!drag || event.pointerId !== drag.pointerId) return;
        if (!drag.started && !dragExceeded(drag.startX, drag.startY, event.clientX, event.clientY)) return;
        event.preventDefault();
        drag.started = true;
        record.element.classList.add("is-dragging");
        applyPixelPosition(record, drag.startLeft + event.clientX - drag.startX, drag.startTop + event.clientY - drag.startY);
        const sampled = stableMovementState(drag.lastX, drag.lastY, event.clientX, event.clientY, record.motion);
        if (!sampled.sampled) return;
        drag.lastX = event.clientX;
        drag.lastY = event.clientY;
        if (record.motion === sampled.motion) return;
        record.motion = sampled.motion;
        applyVisualAsset(record);
      });
      button.addEventListener("pointerup", event => finishPointer(record, event, true));
      button.addEventListener("pointercancel", event => finishPointer(record, event, false));
      button.addEventListener("lostpointercapture", event => finishPointer(record, event, false));
      button.addEventListener("keydown", event => {
        const directions = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
        const direction = directions[event.key];
        if (!direction) return;
        event.preventDefault();
        const step = event.shiftKey ? 40 : 12;
        record.motion = movementState(direction[0] * step, direction[1] * step);
        applyPixelPosition(record, record.left + direction[0] * step, record.top + direction[1] * step);
        record.position = normalizedPosition(record.left, record.top, layerBounds(), recordSize(record));
        options.onPositionCommit(record.avatarId, { ...record.position });
        refreshVisual(record);
        clearTimeout(record.motionTimer);
        record.motionTimer = setTimeout(() => {
          record.motion = null;
          refreshVisual(record);
        }, 180);
      });

      applyStatus(record, avatarStatus(avatar));
      applyNormalizedPosition(record, record.position);
      return record;
    }

    function repositionAll() {
      if (destroyed) return;
      records.forEach(record => {
        if (!record.drag?.started) applyNormalizedPosition(record, record.position);
      });
    }

    function scheduleReposition() {
      if (resizeFrame != null) cancelAnimationFrame(resizeFrame);
      resizeFrame = requestAnimationFrame(() => {
        resizeFrame = null;
        repositionAll();
      });
    }

    function update(next = {}) {
      options = { ...options, ...next };
      const avatars = Array.isArray(options.avatars) ? options.avatars : (Array.isArray(options.agents) ? options.agents : []);
      const currentIds = new Set(avatars.map(avatarId).filter(Boolean));
      records.forEach((record, id) => {
        if (currentIds.has(id)) return;
        clearTimeout(record.celebrationTimer);
        clearTimeout(record.motionTimer);
        record.element.remove();
        records.delete(id);
      });
      avatars.forEach((avatar, index) => {
        const id = avatarId(avatar);
        if (!id) return;
        let record = records.get(id);
        if (!record) {
          record = createRecord(avatar, index, options.positions?.[id]);
          records.set(id, record);
        } else {
          record.avatar = avatar;
          record.index = index;
          record.heading.textContent = avatarLabel(avatar);
          record.detail.textContent = avatar.action_label || "查看详情";
          record.element.dataset.presence = avatar.presence || "agent";
          if (avatar.agent_id) record.element.dataset.agentId = avatar.agent_id;
          else delete record.element.dataset.agentId;
          if (!record.drag && options.positions?.[id]) {
            applyNormalizedPosition(record, options.positions[id]);
          }
          applyStatus(record, avatarStatus(avatar));
        }
      });
      root.hidden = avatars.length === 0;
      scheduleReposition();
    }

    function closeFromDocument(event) {
      if (!root.contains(event.target)) records.forEach(record => closeBubble(record));
    }

    const resizeObserver = typeof ResizeObserver === "function" ? new ResizeObserver(scheduleReposition) : null;
    resizeObserver?.observe(root);
    document.addEventListener("pointerdown", closeFromDocument, true);
    update(initial);

    return {
      update,
      destroy() {
        destroyed = true;
        resizeObserver?.disconnect();
        document.removeEventListener("pointerdown", closeFromDocument, true);
        if (resizeFrame != null) cancelAnimationFrame(resizeFrame);
        records.forEach(record => {
          clearTimeout(record.celebrationTimer);
          clearTimeout(record.motionTimer);
        });
        records.clear();
        root.replaceChildren();
      },
    };
  }

  global.FocusPatrolAvatar = Object.freeze({
    mount,
    normalizeStatus,
    statusPresentation,
    sanitizePosition,
    defaultPosition,
    pixelPosition,
    normalizedPosition,
    dragExceeded,
    movementState,
    stableMovementState,
    avatarId,
    avatarStatus,
    avatarLabel,
  });
})(typeof window === "undefined" ? globalThis : window);
