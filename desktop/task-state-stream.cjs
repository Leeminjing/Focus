"use strict";

// A single authenticated stream carries invalidations, never task snapshots.
// Snapshot reconciliation on every connection closes gaps during disconnection.
function subscribeTaskChanges({ url, headers, onChange, fetchImpl = fetch,
  retryMs = 1000, maxRetryMs = 60000, idleMs = 45000 }) {
  const abort = new AbortController();
  let timer, idleTimer;
  const sleep = ms => new Promise(resolve => {
    const done = () => { clearTimeout(timer); abort.signal.removeEventListener("abort", done); resolve(); };
    timer = setTimeout(done, ms);
    timer.unref?.();
    abort.signal.addEventListener("abort", done, { once: true });
    if (abort.signal.aborted) done();
  });
  const finished = (async () => {
    let delay = retryMs;
    while (!abort.signal.aborted) {
      const connection = new AbortController();
      const resetIdle = () => {
        clearTimeout(idleTimer);
        idleTimer = setTimeout(() => connection.abort(), idleMs);
        idleTimer.unref?.();
      };
      try {
        resetIdle();
        const response = await fetchImpl(url, { headers,
          signal: AbortSignal.any([abort.signal, connection.signal]) });
        if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
        onChange();
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        try {
          while (!abort.signal.aborted) {
            const { value, done } = await reader.read();
            if (done) break;
            resetIdle();
            delay = retryMs;
            buffer += decoder.decode(value, { stream: true });
            // The server emits LF framing; accept CRLF framing as well.
            buffer = buffer.replace(/\r\n/g, "\n");
            let boundary;
            while ((boundary = buffer.indexOf("\n\n")) !== -1) {
              const frame = buffer.slice(0, boundary);
              buffer = buffer.slice(boundary + 2);
              if (/^event:\s*changed\s*$/m.test(frame)) onChange();
            }
            if (buffer.length > 65536) throw new Error("任务状态事件过大");
          }
        } finally {
          await reader.cancel().catch(() => {});
          reader.releaseLock();
        }
      } catch (error) {
        if (!abort.signal.aborted) console.warn("任务状态订阅中断:", error.name);
      } finally {
        clearTimeout(idleTimer);
        connection.abort();
      }
      if (!abort.signal.aborted) {
        await sleep(delay);
        delay = Math.min(maxRetryMs, delay * 2);
      }
    }
  })();
  const unsubscribe = () => { abort.abort(); clearTimeout(idleTimer); };
  unsubscribe.finished = finished;
  return unsubscribe;
}

module.exports = { subscribeTaskChanges };
