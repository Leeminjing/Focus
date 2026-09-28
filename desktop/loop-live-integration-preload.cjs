/*
 * 本文件对外提供首轮 Loop 集成验收的 Electron 网络适配器。
 * 输入为隔离后端 URL 与可选首次 Live GET 故障开关；输出为真实 Loop、Console、会话、关联查询和 Live SSE 响应。
 * 具体工作流为复用桌面测试的非 Loop 任务外壳，将 Loop 请求转发到隔离数据库，并按开关注入一次 HTTP 500 验证自动恢复。示例：设置 FOCUS_LOOP_E2E_FAIL_FIRST_LIVE=1 后加载本 preload。
 */
"use strict";

const nativeFetch = window.fetch.bind(window);
require("./agent-loop-test-preload.cjs");
const localFetch = window.fetch;
const backend = process.env.FOCUS_LOOP_E2E_SERVER;
let failFirstLive = process.env.FOCUS_LOOP_E2E_FAIL_FIRST_LIVE === "1";

window.fetch = (input, options = {}) => {
  const url = new URL(String(input), "http://focus.test");
  const path = url.pathname;
  if (failFirstLive && /^\/desktop\/api\/agent-loops\/[^/]+\/live$/.test(path)) {
    failFirstLive = false;
    return Promise.resolve(new Response(JSON.stringify({ detail: "transient live failure" }), {
      status: 500,
      statusText: "Internal Server Error",
      headers: { "Content-Type": "application/json" },
    }));
  }
  if (path.startsWith("/desktop/api/agent-loops/") || path.startsWith("/desktop/api/curation-programs/") ||
      /^\/desktop\/api\/workspaces\/[^/]+\/(?:context-evolution|context-tree|slots)$/.test(path)) {
    return nativeFetch(new URL(`${path}${url.search}`, backend), options);
  }
  return localFetch(input, options);
};
