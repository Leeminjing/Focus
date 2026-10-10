/* 本文件对外提供隔离传统任务验收的原生运行时桥。
 * 输入为测试 HTTP bridge URL；输出为生产页面使用的真实 API base/session。
 * 具体工作流为保留浏览器 fetch，所有业务调用进入隔离 FastAPI，不返回界面业务 fixture。
 * 示例：FOCUS_TASK_REAL_URL 由 Python 验收服务传入。
 */
window.focusDesktop = { runtime: () => ({ apiBase: process.env.FOCUS_TASK_REAL_URL, session: "focus-dev-session" }) };
