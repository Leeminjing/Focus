"""Verify independent memory reading, selection and source provenance in Chromium.

Run: python desktop/tests/e2e/memory-source-selection.browser.py
Uses the offline desktop fixture; FOCUS_MEMORY_SELECTION_QA_DIR enables screenshots.
"""
import os
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

DESKTOP = Path(__file__).resolve().parents[2]
QA = os.environ.get("FOCUS_MEMORY_SELECTION_QA_DIR")

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1680, "height": 1000})
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script((DESKTOP / "tests/fixtures/context-ui-test-preload.cjs").read_text(encoding="utf-8"))
        page.goto((DESKTOP / "src/renderer/index.html").as_uri())
        page.wait_for_load_state("networkidle")
        page.evaluate("""async () => {
            await goFocusHome(); state.inspector.open = false; state.view = 'memory'; openMemoryCompose();
            await pickMemorySession('root');
            state.memory.enabledMessages = [
                {id:'first',role:'human',content:'Same text. Same text. Tail.'},
                {id:'second',role:'ai',content:'A separate answer.'},
                {id:'third',role:'human',content:'A nonadjacent message.'}
            ];
            document.getElementById('appInspector').hidden = true;
            renderMemory();
            window.memoryWrites = [];
            const originalFetch = window.fetch;
            window.fetch = (url, options = {}) => {
                if (String(url).includes('/desktop/api/memory') && options.method && options.method !== 'GET') {
                    memoryWrites.push({url:String(url),body:JSON.parse(options.body)});
                    return Promise.resolve(new Response(JSON.stringify({content:'Compressed result',segments:[]})));
                }
                return originalFetch(url, options);
            };
            window.initialDraft = state.memory.draft;
            window.initialSources = state.memory.collectedSources;
            window.targetInput = document.querySelector('[data-memory-field="content"]');
        }""")
        read = page.locator('[data-action="read-memory-message"]')
        checks = page.locator('[data-memory-message-select]')
        read.nth(0).click()
        expect(checks.nth(0)).not_to_be_checked()
        expect(page.locator('.memory-source-text')).to_have_text('Same text. Same text. Tail.')
        preview = page.locator('.memory-source-text').element_handle()
        checks.nth(2).check()
        checks.nth(0).focus()
        page.keyboard.press('Space')
        expect(checks.nth(0)).to_be_checked()
        assert preview.evaluate('(node) => node.isConnected')
        read.nth(1).click()
        assert page.evaluate("state.memory.selectedMessageIds.join(',')") == 'third,first'
        page.locator('.memory-msg-preview').nth(0).click()
        assert page.evaluate('state.memory.activeMessageId') == 'second'
        assert page.evaluate("state.memory.selectedMessageIds.join(',')") == 'third,first'
        assert page.evaluate('state.memory.draft === initialDraft && state.memory.collectedSources === initialSources && targetInput.isConnected')
        assert page.evaluate('memoryWrites.length') == 0
        # Adding uses precisely the checked nonadjacent message IDs.
        page.locator('[data-memory-field="content"]').fill('User edited target')
        page.locator('[data-action="add-checked-messages"]').click()
        assert page.evaluate("state.memory.collectedSources[0].message_ids.join(',')") == 'third,first'
        expect(page.locator('.memory-source-edit')).to_have_count(1)
        expect(page.locator('[data-memory-field="content"]')).to_have_value('User edited target')
        page.locator('[data-action="pick-session-source"]').click()
        assert page.evaluate("state.memory.collectedSources[1].context_id") == 'root'
        # Select the second occurrence, including its leading space.
        page.locator('[data-action="read-memory-message"]').nth(0).click()
        page.evaluate("""() => {
            const pre = document.querySelector('[data-selectable-text]');
            const range = document.createRange(); range.setStart(pre.firstChild,10); range.setEnd(pre.firstChild,20);
            getSelection().removeAllRanges(); getSelection().addRange(range);
            pre.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
        }""")
        page.locator('[data-action="add-text"]').click()
        text_source = page.evaluate('state.memory.collectedSources[2]')
        assert text_source['message_id'] == 'first'
        assert text_source['text'] == ' Same text'
        assert text_source['ranges'] == [{'start': 10, 'end': 20}]
        assert page.evaluate('memorySourcesForApi(state.memory.collectedSources, state.memory.activeSessionId)') == [
            {'type': 'messages', 'context_id': 'root', 'message_ids': ['third', 'first']},
            {'type': 'session', 'context_id': 'root'},
            {'type': 'text', 'context_id': 'root', 'parts': [{'message_id': 'first', 'ranges': [{'start': 10, 'end': 20}]}]},
        ]
        # Preview changes clear stale character selections, leaving collected sources intact.
        before = page.evaluate('JSON.stringify(state.memory.collectedSources)')
        page.locator('[data-action="read-memory-message"]').nth(2).click()
        assert page.evaluate('state.memory.textMessageId') is None
        assert page.evaluate('JSON.stringify(state.memory.collectedSources)') == before
        assert page.evaluate('memoryWrites.length') == 0
        # Checkboxes, not article bodies or original text, control merge membership.
        page.locator('[data-action="enter-merge-sources"]').click()
        cards = page.locator('.memory-source-edit')
        cards.nth(0).locator('textarea').click()
        assert page.evaluate('state.memory.selectedSourcesForMerge.length') == 0
        merge_checks = page.locator('[data-memory-merge-select]')
        merge_checks.nth(0).check()
        merge_checks.nth(2).focus()
        page.keyboard.press('Space')
        expect(merge_checks.nth(2)).to_be_checked()
        assert page.evaluate('state.memory.selectedSourcesForMerge.join(",")') == '0,2'
        if QA:
            Path(QA).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(QA) / 'memory-selection-wide.png'), full_page=True)
        page.locator('[data-action="confirm-merge-sources"]').click()
        merged = page.evaluate('state.memory.collectedSources[0]')
        assert merged['subSources'][0]['message_ids'] == ['third', 'first']
        assert merged['subSources'][1] == text_source
        assert page.evaluate('memoryWrites.length') == 0
        # Native checkbox focus and checked state survive viewport changes.
        for width in [1000, 700]:
            page.set_viewport_size({'width': width, 'height': 1000})
            page.evaluate('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')
            assert page.locator('[data-memory-message-select]').nth(0).is_checked()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        if QA:
            page.screenshot(path=str(Path(QA) / 'memory-selection-narrow.png'), full_page=True)
        page.evaluate("() => { FocusI18n.setLocale('en-US'); renderMemory(); }")
        expect(page.get_by_role('button', name='Read original', exact=True).first).to_be_visible()
        assert 'Source collection:' in page.locator('.memory-target-sources').inner_text()
        # Only explicit compression and save controls produce writes.
        page.locator('[data-action="recompress-memory"]').click()
        page.wait_for_function('() => state.memory.draft.content === "Compressed result"')
        assert len(page.evaluate('memoryWrites')) == 1
        page.locator('[data-action="save-memory"]').click()
        page.wait_for_function('() => memoryWrites.length === 2')
        assert page.evaluate('memoryWrites[1].body.content') == 'Compressed result'
        assert not errors, errors
        print('PASS: independent reading/checking, native keyboard selection, no preview writes, exact text ranges, merge provenance, responsive layouts, English, explicit compression/save')
    finally:
        browser.close()
