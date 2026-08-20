/* dsh-eyes 前端粘贴处理器:聊天输入框粘贴图片 → data URL 待发队列。

   工作流:
     (1) 监听 document 的 paste 事件;仅当焦点在聊天输入框(#mainInput)且
         剪贴板含图片时捕获;
     (2) 图片 → FileReader → data URL,入队 window.__dshEyesPendingImages;
     (3) 在输入框旁显示「已粘贴 N 张图片」反馈标记;
     (4) app.js sendMain 经 __dshEyesTakePendingImages() 取走并清空队列。
*/
(function (root) {
  "use strict";

  const pending = [];
  root.__dshEyesPendingImages = pending;
  root.__dshEyesTakePendingImages = () => {
    const taken = pending.splice(0, pending.length);
    if (previewBox && previewBox.isConnected) previewBox.remove();
    previewBox = null;
    return taken;
  };

  let previewBox = null;

  // 图片缩略图预览行:显示在 composer 外部上方(插到 composer 之前,
  // 独立于两列 grid,不挤乱输入框;每张可删除;发送时经 __dshEyesTakePendingImages 取走清空)
  function renderPreview(input) {
    const container = input.closest(".composer") || input.parentElement;
    if (!previewBox || !previewBox.isConnected) {
      previewBox = document.createElement("div");
      previewBox.className = "dsh-eyes-paste-preview";
      previewBox.style.cssText = "display:flex; flex-wrap:wrap; gap:8px; align-items:center; padding:4px 2px;";
      const parent = container.parentElement || container;
      parent.insertBefore(previewBox, container);
    }
    previewBox.innerHTML = pending.map((image, index) =>
      `<span class="dsh-eyes-preview-item" style="position:relative;">
        <img src="${escapeAttr(image.url)}" alt="待发送图片" style="height:72px;border-radius:6px;border:1px solid #ddd;display:block;">
        <button type="button" data-remove-image="${index}" style="position:absolute;top:-8px;right:-8px;width:18px;height:18px;border-radius:50%;border:1px solid #ccc;background:#fff;cursor:pointer;font-size:11px;line-height:1;">×</button>
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
    for (const file of images) {
      const reader = new FileReader();
      reader.onload = () => {
        pending.push({ url: String(reader.result), name: file.name || "pasted-image" });
        renderPreview(input);
      };
      reader.readAsDataURL(file);
    }
  });
})(typeof globalThis === "object" ? globalThis : this);
