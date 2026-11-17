const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'frontend', 'services', 'static', 'camera_rotation.js'), 'utf8');

function start(initial) {
    const storage = new Map(initial == null ? [] : [['dabom.cameraRotationDegrees', initial]]);
    const wrapper = {
        clientWidth: 800, clientHeight: 450, dataset: {}, vars: {},
        style: { setProperty(name, value) { wrapper.vars[name] = value; } },
    };
    const image = { src: '/video_feed' };
    const button = {
        events: {}, textContent: '', title: '', attrs: {},
        addEventListener(type, callback) { this.events[type] = callback; },
        setAttribute(name, value) { this.attrs[name] = value; },
    };
    const docEvents = {};
    let observer;
    const context = {
        document: {
            getElementById(id) { return { 'video-wrapper': wrapper, 'camera-stream': image, 'camera-rotate-btn': button }[id]; },
            addEventListener(type, callback) { docEvents[type] = callback; },
        },
        window: {
            localStorage: {
                getItem(key) { return storage.has(key) ? storage.get(key) : null; },
                setItem(key, value) { storage.set(key, value); },
            },
            addEventListener() {},
        },
        ResizeObserver: class {
            constructor(callback) { observer = callback; }
            observe(element) { assert.equal(element, wrapper); }
        },
    };
    vm.runInNewContext(source, context);
    return { wrapper, image, button, docEvents, storage, resize: () => observer() };
}

const view = start(null);
assert.equal(view.wrapper.dataset.cameraRotation, '0');
assert.equal(view.button.textContent, '↻ 0°');
for (const angle of [90, 180, 270, 0]) {
    view.button.events.click();
    assert.equal(view.wrapper.dataset.cameraRotation, String(angle));
    assert.equal(view.button.textContent, `↻ ${angle}°`);
    assert.equal(view.storage.get('dabom.cameraRotationDegrees'), String(angle));
}
assert.equal(view.image.src, '/video_feed', 'Streaming URL must remain unchanged');

view.button.events.click(); // 90 degrees
assert.equal(view.wrapper.vars['--camera-rotation-width'], '800px');
assert.equal(view.wrapper.vars['--camera-rotation-height'], '450px');
view.wrapper.clientWidth = 1280;
view.wrapper.clientHeight = 720;
view.resize();
assert.equal(view.wrapper.vars['--camera-rotation-width'], '1280px');
assert.equal(view.wrapper.vars['--camera-rotation-height'], '720px');
view.docEvents.fullscreenchange();

const persisted = start('270');
assert.equal(persisted.wrapper.dataset.cameraRotation, '270');
assert.match(persisted.button.attrs['aria-label'], /현재 270도/);
const invalid = start('999');
assert.equal(invalid.wrapper.dataset.cameraRotation, '0');

console.log('PASS: camera rotation cycle, fullscreen resize, persistence, stream unchanged');
