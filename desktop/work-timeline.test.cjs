"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");
const { createElement } = require("./test-dom.cjs");
const Timeline = require("./work-timeline.js");
const View = require("./workspace-patrol-view.js");
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes; }); return { promise, resolve }; };
const entry = (sequence, revision = 1) => ({ entry_id: `entry-${sequence}`, sequence, category: "fact", kind: "fact.upserted", fact_id: "one-test", fact_revision: revision, title: sequence === 2 ? "验证通过 · 8 passed" : "8 passed", recorded_at: "2026-10-21T02:00:00Z", context_ids: ["ctx"], references: { state: revision === 1 ? "observed" : "verified" } });

function fixture(timeline) {
  const root = vm.createContext({ document: { createElement }, setTimeout, clearTimeout });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "work-timeline.js"), "utf8"), root);
  const host = createElement("div");
  host.innerHTML = View.skeleton({ workspace_id: "w", display_name: "w" });
  const card = host.querySelector(".patrol-facts-card"), stream = card.querySelector("[data-patrol-facts]");
  stream.scrollTop = 0; stream.scrollHeight = 600; stream.clientHeight = 200;
  const controller = root.FocusWorkTimeline.create({ host: card, api: { timeline }, signal: new AbortController().signal });
  const click = selector => card.dispatchEvent({ type: "click", target: card.querySelector(selector), stopPropagation() {} });
  return { controller, card, stream, click };
}

test("timeline uses event identities and keeps observation and verification distinct", () => {
  const html = [entry(1), entry(2, 2)].map(Timeline.entryHtml).join("");
  assert.match(html, /data-timeline-entry="entry-1"/);
  assert.match(html, /data-timeline-entry="entry-2"/);
  assert.match(html, /data-fact-revision="1"/);
  assert.match(html, /data-fact-revision="2"/);
  assert.doesNotMatch(Timeline.entryHtml({ ...entry(1), title: '<img src=x onerror="bad()">', summary: "<script>bad()</script>" }), /<img|<script/);
});

test("incremental events keep existing evidence DOM and scroll position", async () => {
  let reads = 0;
  const f = fixture(async (_id, options) => {
    reads++;
    return options.after === undefined
      ? { entries: [entry(1)], next_after: 1, next_before: null, projected_through: 1, source_sequence: 1 }
      : { entries: [entry(2, 2)], next_after: 2, next_before: null, projected_through: 2, source_sequence: 2 };
  });
  try {
    f.controller.sync("loop", {last_sequence:1}); await tick();
    const first = f.stream.querySelector('[data-timeline-entry="entry-1"]');
    const evidence = first.querySelector("[data-timeline-evidence]"); evidence.open = true;
    const body = evidence.querySelector("[data-timeline-evidence-body]"); body.textContent = "已展开的历史证据";
    f.stream.scrollTop = 30;
    f.controller.sync("loop", {last_sequence:2}); await tick();
    assert.equal(f.stream.querySelector('[data-timeline-entry="entry-1"]'), first);
    assert.equal(first.querySelector("[data-timeline-evidence]"), evidence);
    assert.equal(evidence.open, true);
    assert.equal(body.textContent, "已展开的历史证据");
    assert.equal(f.stream.scrollTop, 30);
    assert.equal(f.stream.querySelectorAll("[data-timeline-entry]").length, 2);
    assert.equal(reads, 2);
    assert.equal(f.card.querySelector("[data-patrol-new-facts]").hidden, false);
  } finally { f.controller.dispose(); }
});

test("filter resets reject a delayed response and query the whole history", async () => {
  const delayed = deferred(), requests = [];
  const f = fixture(async (id, options) => {
    requests.push(options);
    if (requests.length === 1) return delayed.promise;
    return { entries: [entry(2,2)], next_after: 2, projected_through:2,source_sequence:2 };
  });
  try {
    f.controller.sync("loop", null);
    f.click('[data-timeline-category="fact"]'); await tick();
    delayed.resolve({entries:[entry(1)],next_after:1,projected_through:1,source_sequence:1}); await tick();
    assert.equal(requests[1].category, "fact");
    assert.equal(requests[1].after, undefined);
    assert.equal(f.stream.querySelectorAll("[data-timeline-entry]").length, 1);
    assert.equal(f.stream.querySelector("[data-timeline-entry]").dataset.timelineEntry, "entry-2");
  } finally { f.controller.dispose(); }
});

test("read failures preserve history and expose an explicit retry", async () => {
  let fail = false;
  const f = fixture(async () => { if (fail) throw new Error("503"); return {entries:[entry(1)],next_after:1,projected_through:1,source_sequence:1}; });
  try {
    f.controller.sync("loop", {last_sequence:1}); await tick(); fail = true;
    const first = f.stream.querySelector("[data-timeline-entry]");
    f.controller.sync("loop", {last_sequence:2}); await tick();
    assert.equal(f.stream.querySelector("[data-timeline-entry]"), first);
    assert.equal(f.card.querySelector("[data-timeline-retry]").hidden, false);
    fail = false; f.click("[data-timeline-retry]"); await tick();
    assert.equal(f.card.querySelector("[data-timeline-retry]").hidden, true);
  } finally { f.controller.dispose(); }
});

test("an authority change clears cached history even when the journal sequence stays the same", async () => {
  let restricted = false;
  const f = fixture(async () => ({ entries: restricted ? [] : [entry(1)], next_after:1, projected_through:1, source_sequence:1 }));
  try {
    f.controller.sync("loop", {last_sequence:1,loop:{state:{authority_revision:1}}}); await tick();
    assert.equal(f.stream.querySelectorAll("[data-timeline-entry]").length, 1);
    restricted = true;
    f.controller.sync("loop", {last_sequence:1,loop:{state:{authority_revision:2}}}); await tick();
    assert.equal(f.stream.querySelectorAll("[data-timeline-entry]").length, 0);
  } finally { f.controller.dispose(); }
});
