// 承诺层 CDP 自动化测试：驱动 Focus 桌面端完整跑一遍 /commit 九阶段流程
// 用法：node cdp-commitment-test.mjs [指令文件] [反馈文件]
import { readFileSync } from "node:fs";

const CDP_PORT = Number(process.env.CDP_PORT || 9223);
const INSTRUCTION = readFileSync(process.argv[2] || "commit-instruction.txt", "utf8").trim();
const RESOLUTION_FEEDBACK = readFileSync(process.argv[3] || "commit-resolution.txt", "utf8").trim();
const STAGE_TIMEOUT_MS = 8 * 60 * 1000; // 每阶段最长 8 分钟（模型调用可能较慢）
const POLL_MS = 2000;

const log = (...args) => console.log(`[${new Date().toISOString().slice(11, 19)}]`, ...args);

async function getPageTarget() {
  for (let i = 0; i < 30; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${CDP_PORT}/json`);
      const targets = await res.json();
      const page = targets.find(t => t.type === "page" && t.url.includes("/desktop/"));
      if (page) return page;
    } catch {}
    await new Promise(r => setTimeout(r, 1000));
  }
  throw new Error("找不到 Focus 页面 target");
}

class CDP {
  constructor(wsUrl) {
    this.ws = new WebSocket(wsUrl);
    this.id = 0;
    this.pending = new Map();
  }
  async open() {
    await new Promise((resolve, reject) => {
      this.ws.onopen = resolve;
      this.ws.onerror = reject;
      this.ws.onmessage = ev => {
        const msg = JSON.parse(ev.data);
        if (msg.id && this.pending.has(msg.id)) {
          const { resolve, reject } = this.pending.get(msg.id);
          this.pending.delete(msg.id);
          msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
        }
      };
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async evaluate(expression) {
    const res = await this.send("Runtime.evaluate", {
      expression, returnByValue: true, awaitPromise: true,
    });
    if (res.exceptionDetails) {
      throw new Error("页面执行异常: " + JSON.stringify(res.exceptionDetails.exception?.description || res.exceptionDetails));
    }
    return res.result?.value;
  }
  close() { try { this.ws.close(); } catch {} }
}

const pageState = () => `(() => {
  const progress = document.querySelector('#commitmentProgress');
  const review = document.querySelector('.review-panel');
  const trace = document.querySelector('.trace-panel');
  const status = document.querySelector('#globalStatus')?.textContent || '';
  return {
    progressVisible: progress ? !progress.hidden : false,
    progressLabel: document.querySelector('#progressLabel')?.textContent || '',
    progressDone: progress ? progress.querySelectorAll('li.done').length : 0,
    progressActive: progress ? progress.querySelectorAll('li.active').length : 0,
    traceCount: trace ? trace.querySelectorAll('.trace-item').length : 0,
    traceState: trace ? trace.querySelector('.trace-state')?.textContent : '',
    review: review ? {
      stage: review.dataset.stage,
      kicker: review.querySelector('.review-kicker')?.textContent || '',
      title: review.querySelector('h3')?.textContent || '',
      error: review.querySelector('.review-error')?.textContent || '',
      approveVisible: review.querySelector('.approve-button') ? !review.querySelector('.approve-button').hidden : false,
      approveText: review.querySelector('.approve-button')?.textContent || '',
      hasEditor: review.querySelector('.review-contract-editor') ? !review.querySelector('.review-contract-editor').hidden : false,
      submitted: review.classList.contains('review-submitted'),
      draftPreview: (review.querySelector('.review-draft')?.textContent || '').slice(0, 120),
    } : null,
    status,
    conversationLength: document.querySelectorAll('#conversation .message').length,
  };
})()`;

const setInput = instr => `(() => {
  const ta = document.querySelector('[data-skill-input="main"]');
  if (!ta) return 'NO_TEXTAREA';
  ta.value = ${JSON.stringify(instr)};
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  return 'OK';
})()`;

const submitRevise = text => `(() => {
  const panel = document.querySelector('.review-panel');
  if (!panel) return 'NO_PANEL';
  const toggle = panel.querySelector('.revise-toggle');
  if (toggle && !toggle.hidden) toggle.click();
  const input = panel.querySelector('.revision-input');
  if (!input) return 'NO_INPUT';
  input.value = ${JSON.stringify(text)};
  input.dispatchEvent(new Event('input', { bubbles: true }));
  panel.querySelector('.revision-form').requestSubmit();
  return 'SUBMITTED';
})()`;

const approve = () => `(() => {
  const btn = document.querySelector('.review-panel .approve-button');
  if (!btn || btn.hidden) return 'NO_APPROVE';
  btn.click();
  return 'CLICKED';
})()`;

async function waitFor(cdp, predicate, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const state = await cdp.evaluate(pageState());
    if (predicate(state)) return state;
    await new Promise(r => setTimeout(r, POLL_MS));
  }
  throw new Error(`等待超时: ${label}`);
}

async function main() {
  log("CDP 测试开始");
  const target = await getPageTarget();
  log(`连接 target: ${target.url}`);
  const cdp = new CDP(target.webSocketDebuggerUrl);
  await cdp.open();
  log("CDP 已连接");

  // 1. 输入 /commit 指令并发送（必须带 /commit 前缀才会触发承诺层）
  const fullInstruction = "/commit " + INSTRUCTION;
  log("注入指令（长度 " + fullInstruction.length + "）…");
  const setResult = await cdp.evaluate(setInput(fullInstruction));
  if (setResult !== "OK") throw new Error("无法设置输入: " + setResult);
  await cdp.evaluate(`document.querySelector('[data-action="send-main"]').click()`);
  log("已点击发送");

  // 2. 等待承诺流程开始（进度条出现）
  const start = await waitFor(cdp, s => s.progressVisible, 60_000, "承诺流程启动");
  log(`流程启动: ${start.progressLabel} (trace=${start.traceCount})`);

  // 3. 主循环：跟踪进度/审批
  let lastStage = 0;
  let reviewsHandled = 0;
  const timeline = [];
  const deadline = Date.now() + 25 * 60 * 1000; // 全程最长 25 分钟

  while (Date.now() < deadline) {
    const state = await cdp.evaluate(pageState());
    const stage = Number(state.progressDone) || 0;
    if (stage !== lastStage) {
      lastStage = stage;
      log(`进度推进: 阶段 ${stage} ${state.progressLabel} | trace=${state.traceCount} | 对话消息=${state.conversationLength}`);
      timeline.push({ t: new Date().toISOString().slice(11, 19), stage, label: state.progressLabel });
    }

    // 审批面板处理
    if (state.review && !state.review.submitted) {
      const r = state.review;
      reviewsHandled += 1;
      log(`>>> 审批面板 #${reviewsHandled}: 阶段 ${r.stage} ${r.title} | approve=${r.approveVisible} | err=${r.error.slice(0, 80)}`);
      timeline.push({ t: new Date().toISOString().slice(11, 19), stage: r.stage, action: "review" });

      if (!r.approveVisible) {
        // 只能修订（阶段 2 冲突 / reviewed_failed）→ 提交解决矛盾的反馈
        log(`提交修订反馈（解决矛盾）…`);
        const res = await cdp.evaluate(submitRevise(RESOLUTION_FEEDBACK));
        log(`修订提交: ${res}`);
        if (res !== "SUBMITTED") throw new Error("修订提交失败: " + res);
      } else if (r.stage === "7") {
        // 阶段 7：编辑器未改动直接批准（确认并写入）
        log("阶段 7 批准（合同编辑器未改动）…");
        const res = await cdp.evaluate(approve());
        log(`批准: ${res}`);
      } else {
        log(`阶段 ${r.stage} 批准…`);
        const res = await cdp.evaluate(approve());
        log(`批准: ${res}`);
      }
    }

    // 完成判定：进度条隐藏且无审批面板（流程结束，lead 继续）
    if (!state.progressVisible && !state.review && state.traceCount === 0 && state.status && !state.status.includes("等待确认") && state.status !== "正在连接…") {
      const idle = await cdp.evaluate(pageState());
      if (!idle.progressVisible && !idle.review) {
        log("流程结束判定：无进度条、无审批面板");
        break;
      }
    }
    await new Promise(r => setTimeout(r, POLL_MS));
  }

  // 4. 最终状态
  const final = await cdp.evaluate(pageState());
  log("=== 最终状态 ===");
  log(JSON.stringify(final, null, 2));
  log("=== 时间线 ===");
  for (const t of timeline) log(JSON.stringify(t));
  if (final.review && !final.review.submitted) {
    log("⚠️ 仍有未处理的审批面板（流程未完成）");
    process.exitCode = 1;
  } else {
    log("✅ 测试结束");
  }
  cdp.close();
}

main().catch(err => { console.error("测试失败:", err.message); process.exit(1); });
