/* 在无头 Chromium 中加载真实桌面页面，验证附件卡、粘贴/文件输入与发送绑定。
 * 接口由已有 Context fixture 加可控上传回执模拟，不调用真实模型。
 * 运行：node desktop/composer-attachments.browser.cjs（需要可解析的 playwright）。
 * 可选 FOCUS_PLAYWRIGHT_MODULE、FOCUS_BROWSER_EXECUTABLE、FOCUS_COMPOSER_QA_DIR。
 */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { chromium } = require(process.env.FOCUS_PLAYWRIGHT_MODULE || "playwright");

(async () => {
  const browser = await chromium.launch({ headless: true,
    ...(process.env.FOCUS_BROWSER_EXECUTABLE ? { executablePath: process.env.FOCUS_BROWSER_EXECUTABLE } : {}) });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    page.setDefaultTimeout(8000);
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const installAttachmentHarness = () => {
      const baseFetch = window.fetch;
      window.attachmentHarness = { uploads: [], requests: [] };
      window.fetch = async (url, options = {}) => {
        const pathname = new URL(String(url), "http://focus.test").pathname;
        const h = window.attachmentHarness;
        if (pathname.endsWith("/materials/upload")) {
          return new Promise(resolve => h.uploads.push({ file: options.body.get("file"), resolve }));
        }
        if (pathname.endsWith("/main/runs")) {
          h.requests.push(JSON.parse(options.body));
          return new Response(JSON.stringify({ intent_kind: "direct_message", intent_id: `intent-${h.requests.length}` }), { status: 200 });
        }
        return baseFetch(url, options);
      };
    };
    await page.addInitScript({ content: fs.readFileSync(path.join(__dirname, "context-ui-test-preload.cjs"), "utf8")
      + `\n;(${installAttachmentHarness.toString()})();` });
    await page.goto(pathToFileURL(path.join(__dirname, "index.html")).href);
    await page.waitForLoadState("networkidle");
    await page.locator('[data-action="focus-home"]').click();
    await page.locator("#mainInput").waitFor();
    const paste = async name => page.evaluate(name => {
      const bytes = Uint8Array.from(atob("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jD1sAAAAASUVORK5CYII="), char => char.charCodeAt(0));
      const clipboard = new DataTransfer();
      clipboard.items.add(new File([bytes], name, { type: "image/png" }));
      document.querySelector("#mainInput").dispatchEvent(new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: clipboard }));
    }, name);
    const finishUpload = async (index, materialId, failure = false) => page.evaluate(({ index, materialId, failure }) => {
      const upload = attachmentHarness.uploads[index];
      upload.resolve(new Response(JSON.stringify(failure ? { detail: "模拟网络失败" } : {
        material_id: materialId, relative_path: upload.file.name, is_image: true, size_bytes: upload.file.size,
      }), { status: failure ? 500 : 200 }));
    }, { index, materialId, failure });
    const screenshot = async name => {
      if (!process.env.FOCUS_COMPOSER_QA_DIR) return;
      fs.mkdirSync(process.env.FOCUS_COMPOSER_QA_DIR, { recursive: true });
      await page.locator(".composer-shell").screenshot({ path: path.join(process.env.FOCUS_COMPOSER_QA_DIR, name) });
    };

    await paste("error.png");
    assert.match(await page.locator("#composerAttachments").innerText(), /准备中/);
    await page.locator('[data-action="composer-preview-attachment"]').click();
    await page.locator(".image-lightbox img").waitFor();
    await page.locator('.image-lightbox [data-action="close-lightbox"]').click();
    await page.locator("#mainInput").fill("看看这个报错");
    await page.locator("#mainInput").press("Enter");
    await page.waitForFunction(() => document.querySelector('[data-action="send-main"]').textContent.includes("等待附件"));
    assert.equal(await page.evaluate(() => attachmentHarness.requests.length), 0);
    await screenshot("attachments-preparing.png");
    // 发送等待期间新添的文件属于下一条。
    await page.locator("#fileInput").setInputFiles({ name: "next.txt", mimeType: "text/plain", buffer: Buffer.from("next") });
    await page.waitForFunction(() => attachmentHarness.uploads.length === 2);
    await finishUpload(1, "next");
    await finishUpload(0, "image");
    await page.waitForFunction(() => attachmentHarness.requests.length === 1 && !document.querySelector("#mainInput").value);
    const request = await page.evaluate(() => attachmentHarness.requests[0]);
    assert.equal(request.message, "看看这个报错");
    assert.deepEqual(request.material_inputs, [{ material_id: "image", note: "" }]);
    assert.match(await page.locator("#composerAttachments").innerText(), /next.txt/);
    assert.doesNotMatch(await page.locator("#composerAttachments").innerText(), /error.png/);
    await screenshot("attachments-ready.png");
    await page.locator('[data-action="composer-remove-attachment"]').click();
    assert.equal(await page.locator(".composer-attachment").count(), 0);

    await paste("retry.png");
    await page.waitForFunction(() => attachmentHarness.uploads.length === 3);
    await page.locator("#mainInput").fill("重试这张图");
    await page.locator('[data-action="send-main"]').click();
    await finishUpload(2, "retry", true);
    await page.locator(".composer-attachment.is-failed").waitFor();
    assert.equal(await page.evaluate(() => attachmentHarness.requests.length), 1);
    assert.equal(await page.locator("#mainInput").inputValue(), "重试这张图");
    await screenshot("attachments-failed.png");
    await page.locator('[data-action="composer-retry-attachment"]').click();
    await page.waitForFunction(() => attachmentHarness.uploads.length === 4);
    await finishUpload(3, "retried");
    await page.locator(".composer-attachment.is-ready").waitFor();
    await page.setViewportSize({ width: 900, height: 760 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.locator('[data-action="close-inspector"]').click();
    await page.locator("#appInspector").waitFor({ state: "hidden" });
    await screenshot("attachments-narrow.png");
    await page.locator('[data-action="send-main"]').click();
    await page.waitForFunction(() => attachmentHarness.requests.length === 2 && !document.querySelector("#mainInput").value);
    assert.deepEqual(await page.evaluate(() => attachmentHarness.requests[1].material_inputs), [{ material_id: "retried", note: "" }]);
    assert.equal(await page.locator(".composer-attachment").count(), 0);
    assert.deepEqual(errors, []);
    console.log("COMPOSER_ATTACHMENTS_BROWSER_PASS");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
