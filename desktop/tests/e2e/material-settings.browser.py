"""Verify current material selections, persistent policies and Run-owned diffs.

Run: python desktop/tests/e2e/material-settings.browser.py [screenshot directory]
Uses production UI with deterministic API fixtures, without model or database calls.
"""
from pathlib import Path
import os
import sys
import tempfile

from playwright.sync_api import expect, sync_playwright

DESKTOP = Path(__file__).resolve().parents[2] / 'src/renderer'
OUTPUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix='focus-material-qa-'))
OUTPUT.mkdir(parents=True, exist_ok=True)
HARNESS = r"""
(() => {
  const base = window.fetch;
  const reply = value => new Response(JSON.stringify(value), {headers: {'Content-Type': 'application/json'}});
  window.materialHarness = {calls: [], run: 'run-material-123456789', materials: [
    {material_id:'protected',relative_path:'rules.md',material_kind:'text',size_bytes:100,
      reading_mode:'full',instruction_mode:'strict',retention:'irreplaceable',needs_confirmation:true},
    {material_id:'image',relative_path:'diagram.png',material_kind:'image',is_image:true,size_bytes:68,retention:'irreplaceable'},
    {material_id:'optional',relative_path:'notes.txt',material_kind:'text',size_bytes:40,
      reading_mode:'rough',instruction_mode:'reference',retention:'removable'}]};
  window.fetch = async (url, options={}) => {
    const path = new URL(String(url), 'http://focus.test').pathname;
    const body = options.body ? JSON.parse(options.body) : null;
    materialHarness.calls.push({path,method:options.method || 'GET',body});
    if (path === '/desktop/api/plugins') return reply({plugins:[]});
    if (path.endsWith('/browser/present')) return reply({});
    if (path === '/desktop/api/tasks/root/materials') return reply(materialHarness.materials);
    if (path === '/desktop/api/tasks/root/material-history') return reply([
      {material_id:'protected',run_id:'previous-run',relative_path:'rules.md',note:'',current_available:true}]);
    if (path.endsWith('/image-validity')) return reply({valid:true});
    if (path.endsWith('/image/content')) {
      const bytes = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6P3sAAAAASUVORK5CYII='), c=>c.charCodeAt(0));
      return new Response(bytes, {headers:{'Content-Type':'image/png'}});
    }
    if (path.startsWith('/desktop/api/materials/')) {
      const id = path.split('/')[4]; const material = materialHarness.materials.find(m=>m.material_id === id);
      if (path.endsWith('/versions')) return reply([{version_id:'saved-version',source:'before-clear',created_at:'2026-11-10'}]);
      if (path.endsWith('/clear')) material.size_bytes=0;
      if (path.endsWith('/restore')) { material.size_bytes=100; material.needs_confirmation=false; }
      if (options.method==='PUT') Object.assign(material,body);
      if (options.method==='DELETE') materialHarness.materials=materialHarness.materials.filter(m=>m!==material);
      return reply(material);
    }
    if (path.endsWith('/changes')) return reply({run_id:materialHarness.run,status:'success',files:[
      {path:'src/example.js',operation:'modified',added:1,removed:1,first_key:'first',last_key:'last'}]});
    if (path.includes('/changes/')) return reply({before:{available:true,text:'old line\n'},after:{available:true,text:'new line\n'}});
    return base(url,options);
  };
})();
"""

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, **(
        {'executable_path': os.environ['FOCUS_BROWSER_EXECUTABLE']} if os.environ.get('FOCUS_BROWSER_EXECUTABLE') else {}))
    try:
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        page.set_default_timeout(10000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.add_init_script((DESKTOP / '../../tests/fixtures/context-ui-test-preload.cjs').read_text(encoding='utf-8') + HARNESS)
        page.goto((DESKTOP / 'index.html').as_uri())
        page.wait_for_load_state('networkidle')
        page.evaluate("switchTask('root')")
        page.evaluate("openInspector('materials')")
        protected = page.locator('#appInspector [data-material-id="protected"]')
        image = page.locator('#appInspector [data-material-id="image"]')
        optional = page.locator('#appInspector [data-material-id="optional"]')
        expect(protected.get_by_text('版本保护', exact=True)).to_be_visible()
        expect(protected.get_by_text('检测到外部删除', exact=True)).to_be_visible()
        expect(protected.get_by_role('combobox')).to_have_count(0)
        expect(protected.get_by_text('完整阅读', exact=True)).to_have_count(0)
        expect(optional.get_by_text('可移除', exact=True)).to_have_count(0)

        protected.get_by_role('button', name='材料设置', exact=True).click()
        rules = protected.get_by_role('group', name='长期规则', exact=True)
        expect(rules.get_by_label('阅读方式')).to_have_value('full')
        expect(rules.get_by_label('指令规则')).to_have_value('strict')
        expect(rules.get_by_label('保留规则')).to_have_value('irreplaceable')
        expect(protected.get_by_role('button', name='删除', exact=True)).to_have_count(0)
        rules.get_by_label('阅读方式').select_option('rough')
        page.locator('#mainInput').fill('保留这份会话草稿')
        protected.get_by_role('button', name='本轮使用', exact=False).click()
        expect(protected.locator('[data-action="toggle-run-material"]')).to_have_attribute('aria-pressed','true')
        expect(protected.locator('[data-action="toggle-run-material"]')).to_be_focused()
        expect(rules.get_by_label('阅读方式')).to_have_value('rough')
        protected.get_by_role('textbox', name='本轮备注', exact=True).fill('只对本轮生效')
        protected.locator('[data-action="toggle-run-material"]').click()
        expect(protected.get_by_role('textbox', name='本轮备注', exact=True)).to_have_count(0)
        expect(rules.get_by_label('阅读方式')).to_have_value('rough')
        assert not page.evaluate("materialHarness.calls.some(c=>c.method==='DELETE'||c.method==='PUT')")
        protected.locator('[data-action="toggle-run-material"]').click()
        expect(protected.get_by_role('textbox', name='本轮备注', exact=True)).to_have_value('只对本轮生效')
        protected.get_by_role('button',name='保存规则',exact=True).click()
        page.wait_for_function("() => materialHarness.calls.some(c=>c.method==='PUT')")
        assert page.evaluate("materialHarness.calls.find(c=>c.method==='PUT').body") == {
            'reading_mode':'rough','instruction_mode':'strict','retention':'irreplaceable'}
        expect(protected.locator('[data-action="toggle-run-material"]')).to_have_attribute('aria-pressed','true')
        expect(page.locator('#mainInput')).to_have_value('保留这份会话草稿')
        expect(protected).not_to_contain_text('无备注')
        page.screenshot(path=str(OUTPUT/'material-rules.png'),full_page=True)

        protected.get_by_role('button',name='版本',exact=True).click()
        expect(protected.get_by_role('button',name='恢复',exact=True)).to_be_visible()
        protected.locator('[data-action="toggle-run-material"]').click()
        expect(protected.get_by_role('button',name='恢复',exact=True)).to_be_visible()
        page.once('dialog',lambda dialog:dialog.accept())
        protected.get_by_role('button',name='置空',exact=True).click()
        expect(protected.locator('[data-action="toggle-run-material"]')).to_be_disabled()
        expect(protected.get_by_text('版本保护',exact=True)).to_be_visible()
        protected.get_by_role('button',name='版本',exact=True).click()
        protected.get_by_role('button',name='恢复',exact=True).click()
        expect(protected.locator('[data-action="toggle-run-material"]')).to_be_enabled()
        assert page.evaluate("materialHarness.calls.find(c=>c.path.endsWith('/restore')).body.version_id") == 'saved-version'

        # Image preview and mandatory viewing remain separate from inclusion.
        image.get_by_role('button',name='材料设置',exact=True).click()
        expect(image.get_by_role('button',name='删除',exact=True)).to_have_count(0)
        expect(image.get_by_role('button',name='版本',exact=True)).to_be_visible()
        expect(image.locator('[data-action="toggle-image-required"]')).to_be_disabled()
        image.locator('[data-action="toggle-run-material"]').click()
        image.locator('[data-action="toggle-image-required"]').click()
        expect(image.locator('[data-action="toggle-image-required"]')).to_have_attribute('aria-pressed','true')
        assert page.evaluate("materialHarness.calls.some(c=>c.path.endsWith('/image-validity'))")
        image.locator('[data-action="preview-image-material"]').click()
        expect(page.locator('.image-lightbox')).to_be_visible()
        page.locator('.image-lightbox').get_by_role('button',name='关闭',exact=True).click()
        expect(page.locator('.image-lightbox')).to_have_count(0)
        image.locator('[data-action="toggle-run-material"]').click()
        expect(image.locator('[data-action="toggle-image-required"]')).to_have_attribute('aria-pressed','false')
        assert len(page.evaluate('materialHarness.materials')) == 3

        # File metadata shows its exact Run; expanded content still fetches pinned snapshots.
        summary = page.locator('.round-changes-panel > summary')
        expect(summary).to_contain_text('Run run-material-123456789')
        summary.click()
        page.locator('.round-change-file > summary').click()
        expect(page.locator('[data-change-body]')).to_contain_text('old line')
        expect(page.locator('[data-change-body]')).to_contain_text('new line')
        page.locator('[data-review-changes]').click()
        page.wait_for_function("() => document.querySelector('#mainInput').value.includes('Run run-material-123456789')")
        assert page.evaluate("materialHarness.calls.some(c=>c.path.endsWith('/changes/run-material-123456789/first/last'))")
        page.screenshot(path=str(OUTPUT/'run-changes.png'),full_page=True)

        page.evaluate("FocusI18n.setLocale('en-US'); render(); openInspector('materials')")
        expect(image.get_by_role('button',name='Collapse settings',exact=True)).to_be_visible()
        expect(image.locator('.material-selection')).to_have_attribute('aria-label','Use this round: diagram.png')
        page.set_viewport_size({'width':900,'height':850})
        expect(image).to_be_visible()
        assert image.evaluate('el=>el.scrollWidth<=el.clientWidth+1')
        assert not errors, errors
    finally:
        browser.close()
print(f'Material settings and Run changes checks passed; screenshots: {OUTPUT}')
