/*
 * 本文件验证生成中助手正文的增量富文本渲染契约。输入为逐步累积的正文增量，输出为
 * 「流式中即产出 markdown」「稳定段不被后续增量改写」「分段渲染等于整体渲染」三类断言；
 * 工作流复用 test-helper 的 app.js VM 环境，不依赖真实 DOM。
 */
"use strict";

const assert = require("node:assert/strict");
const { createAppHarness, readAppSource } = require("./test-helper.cjs");

const harness = createAppHarness({ globals: { setTimeout } });
harness.vm.runInContext(readAppSource(), harness.context);
const run = code => harness.vm.runInContext(code, harness.context);

// 已闭合的前缀：用于观察「后续追加是否改写了已经呈现的内容」。
const HEAD = "已闭合段落 A。\n\n已闭合段落 B。\n\n";

// 边界构造覆盖：设计文档只实测过前若干类，其余不得假定成立。
const CONSTRUCTS = {
  "普通段落": ["第一句。", "第二句。"],
  "setext 标题": ["标题", "====="],
  "表格": ["| a | b |", "| --- | --- |", "| 1 | 2 |"],
  "未闭合内联": ["**粗体", "结束**"],
  "围栏代码": ["```js", "const a = 1;", "```"],
  "长列表": ["- 一", "- 二", "- 三"],
  "引用块": ["> 引用一", "> 引用二"],
  "缩进代码块": ["    indented code", "    第二行"],
  "HTML 块": ["<div>raw</div>"],
};

// === 任务 1.1：流式中即产出 markdown ===

{
  const html = run(`renderStreamingContent({ text: ${JSON.stringify("# 标题\n\n**粗体** 与 `代码`\n\n- 项一\n- 项二")} })`);
  assert.match(html, /<h1>/, "流式中必须渲染标题结构");
  assert.match(html, /<strong>/, "流式中必须渲染加粗结构");
  assert.match(html, /<li>/, "流式中必须渲染列表结构");
  assert.doesNotMatch(html, /\*\*粗体\*\*/, "标记字符不得作为可见文本出现");
  assert.doesNotMatch(html, /^# 标题/m, "标题标记不得作为可见文本出现");
}

{
  const html = run(`renderStreamingContent({ text: ${JSON.stringify("纯文本，没有任何标记。")} })`);
  assert.match(html, /纯文本，没有任何标记。/, "无标记的正文按普通段落呈现");
}

// === 任务 1.2 / 1.3 / 1.5：稳定段不被改写、边界单调不减 ===

for (const [name, lines] of Object.entries(CONSTRUCTS)) {
  const frames = run(`(() => {
    const head = ${JSON.stringify(HEAD)};
    const lines = ${JSON.stringify(lines)};
    const out = [];
    for (let n = 0; n <= lines.length; n++) {
      const text = head + lines.slice(0, n).join('\\n');
      out.push(streamMarkdownBlocks(text));
    }
    return out;
  })()`);

  const firstFrameCount = frames[0].length;
  for (let i = 1; i < frames.length; i++) {
    assert.ok(
      frames[i].length >= frames[i - 1].length,
      `${name}: 段数必须单调不减（第 ${i} 帧 ${frames[i].length} < 上一帧 ${frames[i - 1].length}）`,
    );
  }
  assert.ok(firstFrameCount >= 2, `${name}: 已闭合前缀应至少切出两个段`);

  const finalFrame = frames[frames.length - 1];
  for (let i = 0; i < frames.length; i++) {
    // 除最后一段（可能仍在增长的暂定尾部）外，其余段的呈现必须与最终一致。
    const stable = frames[i].slice(0, -1);
    assert.deepEqual(
      stable,
      finalFrame.slice(0, stable.length),
      `${name}: 第 ${i} 帧的稳定段被后续增量改写了`,
    );
  }
}

// === 任务 1.4：分段渲染必须等于整体渲染（收敛性）===

for (const [name, lines] of Object.entries(CONSTRUCTS)) {
  const text = HEAD + lines.join("\n");
  const segmented = run(`streamMarkdownBlocks(${JSON.stringify(text)}).join("")`);
  const whole = run(`mdRenderer.render(${JSON.stringify(text)})`);
  assert.equal(segmented, whole, `${name}: 分段渲染结果必须等于整体渲染结果`);
}

// === 任务 2.2：引用式链接定义出现在后文时，逐块渲染仍与整体一致 ===
// 逐块渲染的是全文解析所得 token 的子集；若改成重新 parse 前缀子串，
// 后文才出现的引用定义会丢失，前块的解析结果将与整体渲染不一致。
{
  const text = "见 [文档][ref] 与 [另一处][ref2]。\n\n中间段落。\n\n[ref]: https://example.com/doc\n[ref2]: https://example.com/two";
  const segmented = run(`streamMarkdownBlocks(${JSON.stringify(text)}).join("")`);
  const whole = run(`mdRenderer.render(${JSON.stringify(text)})`);
  assert.equal(segmented, whole, "引用式链接定义位于文末时，逐块渲染仍须等于整体渲染");
  assert.match(segmented, /href="https:\/\/example\.com\/doc"/, "引用式链接应被解析为可点击链接");
  assert.match(segmented, /href="https:\/\/example\.com\/two"/, "后文定义的引用同样应被解析");
}

for (const sample of ["# 标题\n\n正文\n\n- 一\n- 二", "单段文本", "```js\nconst a = 1;\n```\n\n尾段"]) {
  const segmented = run(`streamMarkdownBlocks(${JSON.stringify(sample)}).join("")`);
  const whole = run(`mdRenderer.render(${JSON.stringify(sample)})`);
  assert.equal(segmented, whole, `分段渲染必须等于整体渲染: ${JSON.stringify(sample.slice(0, 20))}`);
}

// === 任务 1.4：完成态与在途最后状态的结构一致 ===

for (const sample of ["# 标题\n\n正文段落。", "**粗体**", "- 一\n- 二"]) {
  const streaming = run(`renderStreamingContent({ text: ${JSON.stringify(sample)} })`);
  const completed = run(`renderAssistantContent(${JSON.stringify(sample)})`);
  assert.ok(
    streaming.includes(completed),
    `在途呈现必须包含与完成态相同的富文本结构: ${JSON.stringify(sample.slice(0, 20))}`,
  );
}

// === 安全语义不得退化（spec: 渲染安全语义不退化）===

{
  const html = run(`renderStreamingContent({ text: ${JSON.stringify('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>')} })`);
  assert.doesNotMatch(html, /<script/i, "原始 script 标记不得被解释为页面标记");
  assert.doesNotMatch(html, /<img[^>]*onerror/i, "原始 img/onerror 不得成为真实元素属性");
  assert.match(html, /&lt;script&gt;/, "原始 HTML 应以转义文本呈现");
}

{
  const html = run(`renderStreamingContent({ text: ${JSON.stringify("[点我](javascript:alert(1))")} })`);
  assert.doesNotMatch(html, /href="javascript:/i, "危险协议不得渲染为可点击链接");
}

console.log("streaming-markdown: 流式富文本渲染、稳定段不被改写、分段等于整体渲染、安全语义通过");
