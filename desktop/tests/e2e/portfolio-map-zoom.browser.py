"""Verify real graph bounds after Fit. Run: python desktop/tests/e2e/portfolio-map-zoom.browser.py."""
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

RENDERER = Path(__file__).resolve().parents[2] / "src/renderer"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_content('<div id="host"></div>')
    for source in ["shared/styles/views.css", "features/map/portfolio-map.css"]:
        page.add_style_tag(path=str(RENDERER / source))
    for source in ["portfolio-map-layout.js", "portfolio-map-view.js"]:
        page.add_script_tag(path=str(RENDERER / "features/map" / source))

    for presentation in ["workbench", "default"]:
        page.evaluate("""presentation => {
            const host = document.querySelector('#host');
            host.className = presentation === 'workbench' ? 'map-portfolio-surface' : '';
            const nodes = Array.from({length:32}, (_,i) => ({context_id:`n-${i}`, title:`Context ${i}`}));
            const edges = nodes.slice(1).map((node,i) => ({source_context_id:nodes[i].context_id, target_context_id:node.context_id}));
            const html = FocusPortfolioMapView.render({nodes,edges}, null, [], {presentation});
            host.innerHTML = presentation === 'workbench'
                ? `<header>32 Context</header><p data-map-status></p><div data-map-graph>${html}</div><div class="workbench-legend"><span>来源关系</span></div>`
                : html;
            FocusPortfolioMapView.bind(host);
        }""", presentation)
        for width, height in [(1000, 600), (320, 240), (800, 300), (1200, 800)]:
            page.locator("#host").evaluate("(host, size) => { host.style.width = size[0]+'px'; host.style.height = size[1]+'px'; }", [width, height])
            page.locator('[data-portfolio-zoom="fit"]').click()
            result = page.evaluate("""() => {
                const scroll = document.querySelector('.portfolio-map-scroll');
                const rect = scroll.getBoundingClientRect();
                const inside = element => {
                    const r = element.getBoundingClientRect();
                    return r.left >= rect.left-1 && r.top >= rect.top-1 && r.right <= rect.right+1 && r.bottom <= rect.bottom+1;
                };
                return {zoom:Number(document.querySelector('.portfolio-map').dataset.zoom),
                    canvasInside:inside(document.querySelector('.portfolio-map-canvas')),
                    nodesInside:[...document.querySelectorAll('.portfolio-context-node')].every(inside),
                    noOverflow:scroll.scrollWidth <= scroll.clientWidth+1 && scroll.scrollHeight <= scroll.clientHeight+1};
            }""")
            assert result["canvasInside"] and result["nodesInside"] and result["noOverflow"], (presentation, width, height, result)
            fitted = result["zoom"]
            page.locator('[data-portfolio-zoom="out"]').click()
            assert page.locator(".portfolio-map").evaluate("map => Number(map.dataset.zoom)") <= fitted
            page.locator('[data-portfolio-zoom="in"]').click()
            assert page.locator(".portfolio-map").evaluate("map => Number(map.dataset.zoom)") > fitted
            page.locator('[data-portfolio-zoom="fit"]').click()
            print(json.dumps({"presentation": presentation, "container": [width, height], **result}))
            if os.environ.get("FOCUS_MAP_ZOOM_EVIDENCE"):
                output = Path(os.environ["FOCUS_MAP_ZOOM_EVIDENCE"])
                output.mkdir(parents=True, exist_ok=True)
                page.locator("#host").screenshot(path=str(output / f"{presentation}-{width}x{height}.png"))
    assert not errors, errors
    browser.close()
    print("Portfolio map Fit: PASS (8 layouts, real canvas/node bounds, zoom direction)")
