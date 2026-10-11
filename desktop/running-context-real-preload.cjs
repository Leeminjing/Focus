/* 本文件对外提供真实浏览器验收的运行时地址与原生 EventSource 观察记录。
 * 输入为隔离服务 URL；输出为生产 focusDesktop.runtime 和事件副本，原生连接、重连及消息均保持不变。
 * 工作流为继承原生 EventSource，只旁听 SSE 帧；示例：Electron preload 后 window.realRunEvents 可供断言读取。
 */
window.focusDesktop = { runtime: () => ({ apiBase: process.env.FOCUS_TASK_REAL_URL, session: "focus-dev-session" }) };
window.realRunEvents = [];
window.realRunSources = [];
const NativeEventSource = window.EventSource;
window.EventSource = class extends NativeEventSource {
  constructor(url, options) {
    super(url, options);
    window.realRunSources.push({ url, source: this });
    for (const type of ["metadata", "tokens", "events", "reasoning", "interrupt", "error", "end"])
      this.addEventListener(type, event => window.realRunEvents.push({ url, type, id: event.lastEventId || "", data: event.data || "" }));
  }
};
