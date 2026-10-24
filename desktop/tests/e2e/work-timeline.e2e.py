"""Browser acceptance using production Timeline/API/view assets and fixture HTTP.

Run: python desktop/tests/e2e/work-timeline.e2e.py --output <directory>
Uses headless Chromium; a fixture route serves local assets without a dev server.
This verifies browser behavior, not a real provider/Agent run.
"""
import argparse
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2] / "src/renderer"

HTML = """<!doctype html><html lang="zh-CN"><meta charset="UTF-8">
<link rel="stylesheet" href="/shared/styles/tokens.css"><link rel="stylesheet" href="/shared/styles/base.css">
<link rel="stylesheet" href="/shared/styles/components.css"><link rel="stylesheet" href="/features/patrol/workspace/workspace-patrol.css">
<style>body{margin:0;background:#f6f8f9;padding:24px;font-family:system-ui,sans-serif;color:#28343e}
main{max-width:760px;margin:auto;--workbench-muted:#60717d}main>.patrol-facts-card{height:790px;width:100%;padding:20px;box-sizing:border-box;background:white;border:1px solid #dfe5e9;border-radius:18px;display:flex;flex-direction:column}
.demo-note{font-size:13px;color:#60717d;margin:0 auto 12px;max-width:760px}#draft{width:100%;box-sizing:border-box;margin-top:12px;min-height:48px}button{cursor:pointer}main>.patrol-facts-card [data-patrol-facts]{padding-top:4px}</style>
<p class="demo-note">Focus · 工作时间线交互验收 · 示例事件</p><main id="timeline"></main><textarea id="draft" aria-label="输入新想法" placeholder="继续输入新想法…"></textarea>
<script src="/shared/http/http-response.js"></script><script src="/features/loop/loop-api.js"></script>
<script src="/features/loop/loop-facts-view.js"></script><script src="/features/patrol/workspace/workspace-patrol-feedback.js"></script><script src="/features/patrol/workspace/workspace-patrol-view.js"></script><script src="/features/files/work-timeline.js"></script>
<script>
const template = document.createElement('template');
template.innerHTML = FocusWorkspacePatrolView.skeleton({workspace_id:'demo',display_name:'Focus'});
const card = template.content.querySelector('.patrol-facts-card'); document.querySelector('main').append(card);
const lifetime = new AbortController();
window.timeline = FocusWorkTimeline.create({host:card,api:FocusLoopApi.create({apiBase:'',session:'test'}),signal:lifetime.signal});
timeline.sync('loop-demo',{last_sequence:9});
</script></html>"""


def sample(sequence, category, title, summary, **values):
    return dict(entry_id=f"e{sequence}", source_event_id=f"source-{sequence}", sequence=sequence,
                kind="fact.upserted" if category == "fact" else "intervention.submitted" if values.get("intent_id") else "patrol.decision.committed",
                category=category, title=title, summary=summary, context_ids=[], references={},
                recorded_at=f"2026-10-21T02:{sequence:02}:00Z", **values)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    rows = [
        sample(1, "user_decision", "用户提出新目标", "开发一个 Obsidian 桌面宠物 Agent", intent_id="idea"),
        sample(2, "user_decision", "Patrol 规划工作方向", "建立需求设计和技术验证两条 Context", round_id="r1", round_number=1, decision_id="d1"),
        sample(3, "context", "Context 自动派生", "需求设计 → UI 交互设计、插件技术验证", round_id="r1", round_number=1),
        sample(4, "fact", "测试结果：8 passed", "已观察到一次测试结果", fact_id="test", fact_revision=1, round_id="r1", round_number=1),
        sample(5, "user_decision", "用户调整开发方向", "增加选中文字并追问 Agent 的能力", intent_id="idea2"),
        sample(6, "user_decision", "Patrol 调整工作安排", "已有 Context 继续运行，同时派生文本交互分支", round_id="r2", round_number=2, decision_id="d2"),
        sample(7, "fact", "验证通过 · 测试结果：8 passed", "验证了先前记录的同一次测试", fact_id="test", fact_revision=2, round_id="r1", round_number=1),
        sample(8, "run", "Agent 执行结束", "基础插件结构已完成", round_id="r1", round_number=1),
        sample(9, "fact", "完成文本引用交互", "选中文字引用功能已实现，待集成验证", fact_id="text", fact_revision=1, round_id="r2", round_number=2),
    ]
    rows[1]["references"] = {"intent_ids": ["idea"]}
    rows[5]["references"] = {"intent_ids": ["idea2"]}
    for row in rows:
        row["context_ids"] = ["context-ui"] if row["sequence"] in {2, 3, 4, 7, 8} else ["context-text"] if row["sequence"] in {6, 9} else []
        if row["category"] == "fact":
            row["references"]["state"] = "verified" if row["fact_revision"] == 2 else "observed"
            row["occurred_at"] = "2026-10-21T02:04:00Z"
    reads, evidence_reads, errors = [], [], []
    fail = {"next": False}

    def respond(route):
        parsed = urlparse(route.request.url)
        params = parse_qs(parsed.query)
        if parsed.path.endswith("/timeline"):
            reads.append(params)
            if fail["next"]:
                fail["next"] = False
                route.fulfill(status=503, body="unavailable")
                return
            visible = list(rows)
            if params.get("category"):
                visible = [r for r in visible if r["category"] == params["category"][0]]
            if params.get("context_id"):
                visible = [r for r in visible if params["context_id"][0] in r["context_ids"]]
            if params.get("round_id"):
                visible = [r for r in visible if r.get("round_id") == params["round_id"][0]]
            if params.get("intent_id"):
                visible = [r for r in visible if r.get("intent_id") == params["intent_id"][0] or params["intent_id"][0] in r["references"].get("intent_ids", [])]
            incremental = "after" in params
            if incremental:
                visible = [r for r in visible if r["sequence"] > int(params["after"][0])]
            if "before" in params:
                visible = [r for r in visible if r["sequence"] < int(params["before"][0])]
            has_more = len(visible) > 4
            visible = visible[:4] if incremental else visible[-4:]
            route.fulfill(json=dict(entries=visible, has_more=has_more, next_before=visible[0]["sequence"] if has_more and not incremental else None,
                                    next_after=visible[-1]["sequence"] if has_more and incremental else rows[-1]["sequence"],
                                    projected_through=rows[-1]["sequence"], source_sequence=rows[-1]["sequence"]))
        elif "/facts/" in parsed.path:
            evidence_reads.append(params)
            route.fulfill(json={"fact": {"fact_id": "test", "kind": "test", "status": "verified", "title": "当前验证结论"},
                                "revisions": [{"revision": 1, "state": "observed", "presentation": {"title": "原始测试观察", "summary": "当时尚未验证"}, "evidence": []},
                                              {"revision": 2, "state": "verified", "presentation": {"title": "后来的验证结果", "summary": "后来验证通过"}, "evidence": []}]})
        elif parsed.path == "/":
            route.fulfill(content_type="text/html", body=HTML)
        else:
            file = (ROOT / parsed.path.lstrip("/")).resolve()
            if not file.is_relative_to(ROOT) or not file.is_file():
                route.fulfill(status=404, body="missing")
            else:
                route.fulfill(path=file, content_type="text/css" if file.suffix == ".css" else "application/javascript")

    with sync_playwright() as p:
        chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
        browser = p.chromium.launch(headless=True, **({"executable_path": str(chrome)} if chrome.exists() else {}))
        page = browser.new_page(viewport={"width": 1000, "height": 1020}, device_scale_factor=1)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("http://timeline.test/**", respond)
        page.goto("http://timeline.test/")
        page.wait_for_load_state("networkidle")
        assert not errors, errors
        expect(page.locator("[data-timeline-entry]")).to_have_count(4)
        page.locator("[data-timeline-older]").click()
        expect(page.locator("[data-timeline-entry]")).to_have_count(8)
        page.locator("[data-timeline-older]").click()
        expect(page.locator("[data-timeline-entry]")).to_have_count(9)
        assert page.locator("[data-timeline-entry]").evaluate_all("nodes=>nodes.map(n=>n.dataset.timelineEntry)") == [f"e{i}" for i in range(1, 10)]
        page.locator('[data-timeline-evidence="e4"] > summary').click()
        expect(page.locator('[data-timeline-evidence="e4"]')).to_have_attribute("data-loaded", "true")
        expect(page.locator('[data-timeline-evidence="e4"]')).to_contain_text("当时尚未验证")
        expect(page.locator('[data-timeline-evidence="e4"]')).not_to_contain_text("后来验证通过")
        assert evidence_reads[-1] == {"revision": ["1"]}
        page.evaluate("window.savedEvidence=document.querySelector('[data-timeline-evidence=e4]');window.savedNode=document.querySelector('[data-timeline-entry=e4]')")
        page.locator("#draft").fill("下一条中文想法")
        page.evaluate("document.querySelector('[data-patrol-facts]').scrollTop=35; window.savedTop=document.querySelector('[data-patrol-facts]').scrollTop")
        rows.append(sample(10, "user_decision", "用户继续补充目标", "保持已展开的历史证据", intent_id="idea3"))
        page.evaluate("timeline.sync('loop-demo',{last_sequence:10})")
        expect(page.locator("[data-timeline-entry]")).to_have_count(10)
        assert page.evaluate("savedEvidence===document.querySelector('[data-timeline-evidence=e4]') && savedEvidence.open && savedNode===document.querySelector('[data-timeline-entry=e4]')")
        assert page.evaluate("document.querySelector('[data-patrol-facts]').scrollTop===savedTop")
        expect(page.locator("#draft")).to_have_value("下一条中文想法")
        assert page.evaluate("document.activeElement.id") == "draft"
        page.locator('[data-timeline-category="fact"]').click()
        expect(page.locator("[data-timeline-entry]")).to_have_count(3)
        page.screenshot(path=str(args.output / "work-timeline-facts.png"), full_page=True)
        page.locator('[data-timeline-category="user_decision"]').click()
        expect(page.locator("[data-timeline-entry]")).to_have_count(4)
        page.locator("[data-timeline-older]").click()
        expect(page.locator("[data-timeline-entry]")).to_have_count(5)
        page.locator('[data-timeline-entry="e1"] [data-timeline-intent]').click()
        expect(page.locator("[data-timeline-entry]")).to_have_count(2)
        expect(page.locator("[data-timeline-scope]")).to_have_text("所选输入及后续决策")
        page.locator("[data-timeline-clear]").click()
        expect(page.locator("[data-timeline-entry]")).to_have_count(4)
        page.locator('[data-timeline-entry="e7"] [data-timeline-context]').click()
        expect(page.locator("[data-timeline-entry]")).to_have_count(4)
        assert reads[-1]["context_id"] == ["context-ui"]
        page.locator("[data-timeline-clear]").click()
        expect(page.locator("[data-timeline-entry]")).to_have_count(4)
        fail["next"] = True
        page.evaluate("timeline.sync('loop-demo',{last_sequence:11})")
        expect(page.locator("[data-timeline-retry]")).to_be_visible()
        expect(page.locator("[data-timeline-entry]")).to_have_count(4)
        page.locator("[data-timeline-retry]").click()
        expect(page.locator("[data-timeline-retry]")).to_be_hidden()
        while page.locator("[data-timeline-older]").is_visible():
            previous_count = page.locator("[data-timeline-entry]").count()
            page.locator("[data-timeline-older]").click()
            page.wait_for_function("n=>document.querySelectorAll('[data-timeline-entry]').length>n", arg=previous_count)
        page.evaluate("document.querySelector('[data-patrol-facts]').scrollTop=0")
        page.screenshot(path=str(args.output / "work-timeline-all.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(args.output / "work-timeline-mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        assert not errors, errors
        page.evaluate("timeline.dispose();lifetime.abort()")
        browser.close()
    print(json.dumps({"passed": True, "timeline_requests": len(reads), "historical_evidence_revision": 1,
                      "screenshots": [str(path) for path in args.output.glob("work-timeline-*.png")]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
