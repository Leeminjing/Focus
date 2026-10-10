/* 本文件对外提供原生页面验收的真实 Patrol 网络适配。
 * 输入为隔离生产路由 URL；输出为真实 Workspace intake、Live、冻结和控制响应。
 * 具体工作流为只对非本次关键路径复用既有页面 fixture，所有 agent-loops、精确 revision 请求走真实 HTTP。
 * 示例：FOCUS_FRONTEND_SERVER 指向本用例隔离服务；没有用户数据库/安装应用/Provider 写入。
 */
const nativeFetch = window.fetch.bind(window);
require("./workspace-patrol-test-preload.cjs");
const localFetch = window.fetch;
window.fetch = (input, options = {}) => {
  const url = new URL(String(input), "http://focus.test");
  if (url.pathname.startsWith("/desktop/api/agent-loops/") || url.pathname.startsWith("/desktop/api/context-revisions/") || /^\/desktop\/api\/contexts\/[^/]+\/revisions$/.test(url.pathname)) {
    return nativeFetch(new URL(url.pathname + url.search, process.env.FOCUS_FRONTEND_SERVER), options);
  }
  return localFetch(input, options);
};
