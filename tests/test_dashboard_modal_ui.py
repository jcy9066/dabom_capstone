import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "frontend" / "components"
TEMPLATE = (ROOT / "frontend" / "templates" / "index.html").read_text(encoding="utf-8")
STYLE = (ROOT / "frontend" / "services" / "static" / "style.css").read_text(encoding="utf-8")


class DashboardModalUiTests(unittest.TestCase):
    def test_saved_map_component_registers_and_renders_through_modal_manager(self):
        source = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

class FakeElement {
    constructor(tag = 'div') {
        this.tagName = tag.toUpperCase();
        this.children = [];
        this.listeners = {};
        this.style = {};
        this.dataset = {};
        this.className = '';
        this.id = '';
        this.name = '';
        this.value = '';
        this.checked = false;
        this.disabled = false;
        this.textContent = '';
        this.classList = {
            toggle: () => {},
            remove: () => {},
        };
    }
    append(...children) { this.children.push(...children); }
    addEventListener(name, handler) { this.listeners[name] = handler; }
    removeAttribute(name) { if (name === 'onclick') this.onclick = null; }
    descendants() { return this.children.flatMap(child => [child, ...child.descendants()]); }
    matches(selector) {
        if (selector.startsWith('#')) return this.id === selector.slice(1);
        if (selector.startsWith('.')) return this.className.split(/\s+/).includes(selector.slice(1));
        if (selector === 'input[name="saved-navigation-map"]:checked') {
            return this.tagName === 'INPUT' && this.name === 'saved-navigation-map' && this.checked;
        }
        if (selector === 'button, input') return this.tagName === 'BUTTON' || this.tagName === 'INPUT';
        return false;
    }
    querySelector(selector) { return this.descendants().find(node => node.matches(selector)) || null; }
    querySelectorAll(selector) { return this.descendants().filter(node => node.matches(selector)); }
}

const trigger = new FakeElement('button');
trigger.onclick = () => {};
const minimap = new FakeElement();
minimap.id = 'minimap-overlay';
const document = {
    createElement: tag => new FakeElement(tag),
    getElementById(id) {
        if (id === 'lidarMapSelectBtn') return trigger;
        if (id === 'minimap-overlay') return minimap;
        return [minimap, ...minimap.descendants()].find(node => node.id === id) || null;
    },
    dispatchEvent() {},
};
const manager = {
    views: new Map(),
    activeView: null,
    body: new FakeElement(),
    register(name, descriptor) { this.views.set(name, descriptor); return this; },
    setBody(content) { this.body = content; },
    close() {
        if (this.activeView) this.views.get(this.activeView.name)?.onClose?.();
        this.activeView = null;
    },
    async open(name, context) {
        this.activeView = { name, context, state: null };
        this.body = await this.views.get(name).render(context, null);
        return this.activeView;
    },
};
const responses = {
    '/api/navigation/maps/active': { ok: true, state: 'active', active_map: { map_name: 'alpha' } },
    '/api/navigation/maps': {
        ok: true,
        maps: [{ map_name: 'alpha', saved_at: '2026-08-28T10:00:00Z', width: 10, height: 20, resolution: 0.05 }],
    },
};
const context = {
    console,
    document,
    CustomEvent: class { constructor(name, options) { this.type = name; this.detail = options.detail; } },
    fetch: async url => ({ ok: true, status: 200, json: async () => responses[url] }),
    setInterval: () => 1,
    clearTimeout,
    setTimeout,
    confirm: () => true,
    prompt: () => null,
};
context.window = context;
context.DabomDashboardComponents = { modal: { getDefault: () => manager } };
vm.createContext(context);
vm.runInContext(fs.readFileSync('frontend/components/navigation/saved_map_modal.js', 'utf8'), context);

(async () => {
    const controller = context.DabomDashboardComponents.navigationMaps.mount({ manager, trigger });
    assert.ok(controller);
    assert.ok(manager.views.has('savedNavigationMaps'));
    assert.strictEqual(trigger.onclick, null);
    await controller.open();
    assert.strictEqual(manager.activeView.name, 'savedNavigationMaps');
    assert.strictEqual(manager.views.get('savedNavigationMaps').title, '\uc800\uc7a5 \uc9c0\ub3c4 \uc120\ud0dd');
    assert.strictEqual(manager.body.querySelector('input[name="saved-navigation-map"]:checked').value, 'alpha');
    assert.strictEqual(manager.body.querySelector('.saved-map-load').disabled, false);
    assert.strictEqual(manager.body.querySelector('#saved-map-pose-x').value, '');
    assert.strictEqual(manager.body.querySelector('.saved-map-active').textContent, 'ACTIVE');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        completed = subprocess.run(
            ["node", "-e", source], cwd=ROOT, capture_output=True, text=True, check=False
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_header_and_backdrop_always_close_the_complete_modal_stack(self):
        source = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

class FakeNode {}
class FakeElement extends FakeNode {
    constructor() {
        super();
        this.listeners = {};
        this.dataset = {};
        this.style = {};
        this.scrollTop = 0;
        this.innerHTML = '';
        this.textContent = '';
    }
    addEventListener(name, handler) { this.listeners[name] = handler; }
    querySelector(selector) { return selector === '[data-modal-close]' ? closeButton : null; }
    replaceChildren(value) { this.child = value; }
    dispatch(name, target = this) {
        this.listeners[name]?.({ target, preventDefault() {} });
    }
}

const closeButton = new FakeElement();
const root = new FakeElement();
const title = new FakeElement();
const body = new FakeElement();
const document = {
    fullscreenElement: null,
    webkitFullscreenElement: null,
    addEventListener() {},
};
const context = { console, Node: FakeNode, document };
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync('frontend/components/modal/modal_manager.js', 'utf8'), context);

const Manager = context.DabomDashboardComponents.modal.ModalManager;
const manager = new Manager({ root, title, body });
manager.register('records', { title: 'records', render: () => 'records' });
manager.register('detail', { title: 'detail', render: () => 'detail' });

(async () => {
    await manager.open('records');
    await manager.open('detail');
    assert.strictEqual(manager.stack.length, 1);
    closeButton.dispatch('click');
    assert.strictEqual(manager.activeView, null);
    assert.strictEqual(manager.stack.length, 0);
    assert.strictEqual(root.style.display, 'none');

    await manager.open('records');
    await manager.open('detail');
    root.dispatch('click', body);
    assert.strictEqual(manager.activeView.name, 'detail');
    root.dispatch('click', root);
    assert.strictEqual(manager.activeView, null);
    assert.strictEqual(manager.stack.length, 0);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        completed = subprocess.run(
            ["node", "-e", source], cwd=ROOT, capture_output=True, text=True, check=False
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_detail_back_is_distinct_from_common_close(self):
        gallery = (COMPONENTS / "gallery" / "image_detail.js").read_text(encoding="utf-8")
        current = (COMPONENTS / "current_situation" / "current_situation.js").read_text(encoding="utf-8")
        modal = (COMPONENTS / "modal" / "modal_manager.js").read_text(encoding="utf-8")

        self.assertIn('data-modal-close', TEMPLATE)
        self.assertNotIn('onclick="closeModal()"', TEMPLATE)
        self.assertIn("if (event.target === this.root) this.close();", modal)
        self.assertIn('data-detail-back>← 이전 화면</button>', gallery)
        self.assertIn("manager.back()", gallery)
        self.assertIn("close() { return manager?.close()", gallery)
        self.assertNotIn("stopImmediatePropagation", current)

        records = (COMPONENTS / "records" / "record_modal.js").read_text(encoding="utf-8")
        controller_close = records.split("            close() {", 1)[1].split("            },", 1)[0]
        self.assertIn("manager.close()", controller_close)
        self.assertNotIn("manager.back()", controller_close)

    def test_toolbar_dom_and_tab_order_matches_visible_order(self):
        toolbar_start = TEMPLATE.index('id="records-toolbar-mount"')
        toolbar_end = TEMPLATE.index("\n            </div>\n        </div>", toolbar_start)
        toolbar = TEMPLATE[toolbar_start:toolbar_end]
        views = ("patrolModal", "galleryModal", "currentSituation", "actionsModal", "statusModal")
        positions = [toolbar.index(f'data-record-view="{view}"') for view in views]

        self.assertEqual(positions, sorted(positions))
        self.assertNotIn('data-record-view="patrolModal"] { order:', STYLE)
        self.assertNotIn('.current-situation-record-btn { order:', STYLE)

    def test_scoped_component_assets_are_cache_busted_consistently(self):
        version = "?v="
        for asset in (
            "/components/modal/modal.css",
            "/components/controls/controls.css",
            "/components/modal/modal_manager.js",
            "/components/navigation/saved_map_modal.js",
            "/components/gallery/image_detail.js",
            "/components/current_situation/current_situation.js",
        ):
            self.assertIn(f'{asset}{version}', TEMPLATE)


if __name__ == "__main__":
    unittest.main()
