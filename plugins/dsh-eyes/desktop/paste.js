/* dsh-eyes 前端粘贴处理器。输入为主 Composer 获得的图片剪贴板项，输出为使用宿主公开令牌的
   待发附件条与 data URL 队列；工作流:
     (1) 监听 document 的 paste 事件;仅当焦点在聊天输入框(#mainInput)且
         剪贴板含图片时捕获;
     (2) 图片 → FileReader → data URL,入队 window.__dshEyesPendingImages;
     (3) 在输入框旁显示「已粘贴 N 张图片」反馈标记;
     (4) app.js sendMain 先 peek，服务确认接收后再 commit 清空队列；失败时附件保持可重试。

   样式只作用于 dsh-eyes 命名空间，不修改宿主布局或发信协议。
*/
(function (root) {
  "use strict";

  const pending = [];
  root.__dshEyesPendingImages = pending;
  root.__dshEyesPeekPendingImages = () => pending.map(image => ({ ...image }));
  root.__dshEyesCommitPendingImages = () => {
    const taken = pending.splice(0, pending.length);
    if (previewBox && previewBox.isConnected) previewBox.remove();
    previewBox = null;
    return taken;
  };
  // 兼容旧宿主；新版宿主使用 peek/commit，避免发送失败丢失附件。
  root.__dshEyesTakePendingImages = root.__dshEyesCommitPendingImages;

  let previewBox = null;

  // 图片缩略图预览行:显示在 composer 外部上方(插到 composer 之前,
  // 独立于两列 grid,不挤乱输入框;每张可删除;发送时经 __dshEyesTakePendingImages 取走清空)
  function renderPreview(input) {
    if (!pending.length) {
      if (previewBox && previewBox.isConnected) previewBox.remove();
      previewBox = null;
      return;
    }
    const container = input.closest(".composer") || input.parentElement;
    if (!previewBox || !previewBox.isConnected) {
      previewBox = document.createElement("div");
      previewBox.className = "dsh-eyes-paste-preview";
      const parent = container.parentElement || container;
      parent.insertBefore(previewBox, container);
    }
    previewBox.innerHTML = pending.map((image, index) =>
      `<span class="dsh-eyes-preview-item">
        <img class="dsh-eyes-preview-image" src="${escapeAttr(image.url)}" alt="待发送图片">
        <button class="dsh-eyes-preview-remove" type="button" data-remove-image="${index}" aria-label="移除第 ${index + 1} 张图片">×</button>
      </span>`
    ).join("");
    previewBox.querySelectorAll("[data-remove-image]").forEach(button => {
      button.addEventListener("click", () => {
        pending.splice(Number(button.dataset.removeImage), 1);
        renderPreview(input);
      });
    });
  }

  function escapeAttr(value) {
    return String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  }

  document.addEventListener("paste", event => {
    const input = event.target.closest && event.target.closest("#mainInput");
    if (!input) return; // 只处理聊天输入框的粘贴
    const items = (event.clipboardData || {}).items || [];
    const images = [];
    for (const item of items) {
      if (item.kind === "file" && item.type.startsWith("image/")) {
        const file = item.getAsFile();
        if (file) images.push(file);
      }
    }
    if (!images.length) return;
    event.preventDefault(); // 图片粘贴不进 textarea 文本
    const ordered = new Array(images.length);
    let remaining = images.length;
    const complete = () => {
      remaining -= 1;
      if (remaining) return;
      pending.push(...ordered.filter(Boolean));
      renderPreview(input);
    };
    images.forEach((file, index) => {
      const reader = new FileReader();
      reader.onload = () => {
        ordered[index] = { url: String(reader.result), name: file.name || "pasted-image" };
        complete();
      };
      reader.onerror = complete;
      reader.readAsDataURL(file);
    });
  });
})(typeof globalThis === "object" ? globalThis : this);
