/*
 * 本文件对外提供首轮 Loop 集成验收的 Electron 网络适配器。
 * 输入为隔离后端 URL；输出为真实 Loop、Console、会话、关联查询和 Live SSE 响应。
 * 具体工作流为复用桌面测试的非 Loop 任务外壳，仅将 Loop 领域请求转发到同一个测试数据库上的生产 HTTP 路由。示例：设置 FOCUS_LOOP_E2E_SERVER 后加载本 preload。
 */
"use strict";

const nativeFetch = window.fetch.bind(window);
require("./agent-loop-test-preload.cjs");
const localFetch = window.fetch;
const backend = process.env.FOCUS_LOOP_E2E_SERVER;

window.fetch = (input, options = {}) => {
  const url = new URL(String(input), "http://focus.test");
  const path = url.pathname;
  if (path.startsWith("/desktop/api/agent-loops/") || path.startsWith("/desktop/api/curation-programs/") ||
      /^\/desktop\/api\/workspaces\/[^/]+\/(?:context-evolution|context-tree|slots)$/.test(path)) {
    return nativeFetch(new URL(`${path}${url.search}`, backend), options);
  }
  return localFetch(input, options);
};
