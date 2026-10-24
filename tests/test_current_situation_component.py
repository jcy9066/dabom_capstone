import subprocess
import textwrap
import unittest


NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}
const tick = () => new Promise(resolve => setImmediate(resolve));

class FakeElement {
    constructor() {
        this.dataset = {};
        this.style = {};
        this.listeners = {};
        this.attributes = {};
        this.textContent = '';
        this.value = '';
        this.disabled = false;
        this.checked = false;
        this.hidden = false;
        this.scrollHeight = 128;
        this.queries = {};
    }
    addEventListener(name, listener) {
        this.listeners[name] = this.listeners[name] || [];
        this.listeners[name].push(listener);
    }
    removeAttribute(name) { delete this.attributes[name]; }
    querySelector(selector) { return this.queries[selector] || null; }
    async emit(name, event = {}) {
        for (const listener of this.listeners[name] || []) await listener(event);
    }
}

function createForm() {
    const elements = {
        form: new FakeElement(),
        preview: new FakeElement(),
        previewState: new FakeElement(),
        description: new FakeElement(),
        includeImage: new FakeElement(),
        error: new FakeElement(),
        submit: new FakeElement(),
        cancel: new FakeElement(),
    };
    elements.preview.hidden = true;
    elements.previewState.className = 'preview-state is-loading';
    elements.includeImage.disabled = true;
    elements.submit.textContent = '기록';
    return elements;
}

const trigger = new FakeElement();
trigger.attributes.onclick = 'openCurrentSituationModal()';
const mountRoot = new FakeElement();
mountRoot.queries['[data-record-view="currentSituation"]'] = trigger;
const closeButton = new FakeElement();
const modalRoot = new FakeElement();
modalRoot.queries['.close-btn'] = closeButton;
const modalTitle = new FakeElement();
const modalBody = new FakeElement();
const forms = [];
let currentForm = null;
modalBody.installForm = () => {
    currentForm = createForm();
    forms.push(currentForm);
};
modalBody.querySelector = selector => ({
    '.current-situation-form': currentForm?.form,
    '#currentSituationPreview': currentForm?.preview,
    '#currentSituationPreviewState': currentForm?.previewState,
    '#currentSituationDescription': currentForm?.description,
    '#currentSituationIncludeImage': currentForm?.includeImage,
    '#currentSituationError': currentForm?.error,
    '#currentSituationSubmit': currentForm?.submit,
    '[data-current-situation-close]': currentForm?.cancel,
}[selector] || null);

let descriptor = null;
const manager = {
    root: modalRoot,
    title: modalTitle,
    body: modalBody,
    activeView: null,
    stack: [],
    register(name, value) {
        assert.strictEqual(name, 'currentSituation');
        descriptor = value;
    },
    async open(name, context, options) {
        assert.strictEqual(name, 'currentSituation');
        assert.strictEqual(options.replace, true);
        this.activeView = { name, context };
        modalBody.innerHTML = await descriptor.render();
        modalBody.installForm();
        modalRoot.style.display = 'flex';
        await descriptor.onOpen(context);
        return this.activeView;
    },
    close() {
        if (this.activeView?.name === 'currentSituation') descriptor.onClose();
        this.activeView = null;
        this.stack.length = 0;
        modalRoot.style.display = 'none';
    },
};

const requests = [];
const revokedUrls = [];
const alerts = [];
const context = {
    console,
    AbortController,
    URL: {
        createObjectURL: blob => `blob:${blob.token}`,
        revokeObjectURL: url => revokedUrls.push(url),
    },
    alert: message => alerts.push(message),
    dashboardRecordsCsrfToken: async () => 'csrf-token',
    fetch: (url, options = {}) => {
        const request = { url, options, ...deferred() };
        requests.push(request);
        return request.promise;
    },
    document: {
        querySelector(selector) {
            if (selector === '#commonModal') return modalRoot;
            if (selector === '#modalTitle') return modalTitle;
            if (selector === '#modalBody') return modalBody;
            return null;
        },
    },
    DabomDashboardComponents: { modal: { getDefault: () => manager } },
};
context.window = context;
vm.createContext(context);
vm.runInContext(
    fs.readFileSync('frontend/components/current_situation/current_situation.js', 'utf8'),
    context,
    { filename: 'current_situation.js' },
);
const controller = context.DabomDashboardComponents.currentSituation.mount(mountRoot, {});

function previewResponse(token) {
    return {
        ok: true,
        status: 200,
        headers: {
            get(name) {
                if (name === 'Content-Type') return 'image/jpeg';
                if (name === 'X-Frame-Token') return token;
                return null;
            },
        },
        blob: async () => ({ token }),
    };
}

function savedResponse() {
    return { status: 201, json: async () => ({ ok: true }) };
}
"""


class CurrentSituationComponentTests(unittest.TestCase):
    def run_scenario(self, scenario: str) -> None:
        source = NODE_HARNESS + "\n" + textwrap.dedent(scenario)
        result = subprocess.run(
            ["node", "-e", source],
            capture_output=True,
            check=False,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_preview_success_defaults_checked_and_textarea_auto_grows(self):
        self.run_scenario(r"""
            (async () => {
                const opening = controller.open();
                await tick();
                assert.strictEqual(requests[0].url, '/api/logs/current-situation/preview');
                requests[0].resolve(previewResponse('frame-a'));
                await opening;

                assert.strictEqual(currentForm.includeImage.disabled, false);
                assert.strictEqual(currentForm.includeImage.checked, true);
                assert.strictEqual(currentForm.description.style.resize, 'none');
                assert.strictEqual(currentForm.description.style.overflowY, 'hidden');
                assert.strictEqual(currentForm.description.style.height, '128px');
                currentForm.description.scrollHeight = 196;
                await currentForm.description.emit('input');
                assert.strictEqual(currentForm.description.style.height, '196px');
            })().catch(error => { console.error(error); process.exitCode = 1; });
        """)

    def test_stale_preview_response_cannot_update_reopened_form(self):
        self.run_scenario(r"""
            (async () => {
                const firstOpen = controller.open();
                await tick();
                const firstRequest = requests[0];
                controller.close();
                assert.strictEqual(firstRequest.options.signal.aborted, true);

                const secondOpen = controller.open();
                await tick();
                const secondRequest = requests[1];
                firstRequest.resolve(previewResponse('stale-frame'));
                await firstOpen;
                assert.strictEqual(currentForm.includeImage.disabled, true);
                assert.strictEqual(currentForm.includeImage.checked, false);
                assert.strictEqual(currentForm.preview.hidden, true);

                secondRequest.resolve(previewResponse('fresh-frame'));
                await secondOpen;
                assert.strictEqual(currentForm.preview.src, 'blob:fresh-frame');
                assert.strictEqual(currentForm.includeImage.checked, true);
            })().catch(error => { console.error(error); process.exitCode = 1; });
        """)

    def test_stale_preview_rejection_cannot_mark_reopened_form_unavailable(self):
        self.run_scenario(r"""
            (async () => {
                const firstOpen = controller.open();
                await tick();
                const firstRequest = requests[0];
                controller.close();
                const secondOpen = controller.open();
                await tick();
                const secondRequest = requests[1];

                firstRequest.reject(new Error('late failure'));
                await firstOpen;
                assert.strictEqual(currentForm.previewState.className, 'preview-state is-loading');
                assert.strictEqual(currentForm.previewState.textContent, '');

                secondRequest.resolve(previewResponse('fresh-after-reject'));
                await secondOpen;
                assert.strictEqual(currentForm.includeImage.checked, true);
            })().catch(error => { console.error(error); process.exitCode = 1; });
        """)

    def test_current_preview_503_uses_unavailable_state(self):
        self.run_scenario(r"""
            (async () => {
                const opening = controller.open();
                await tick();
                requests[0].resolve({
                    ok: false,
                    status: 503,
                    headers: { get: () => null },
                });
                await opening;
                assert.strictEqual(currentForm.preview.hidden, true);
                assert.strictEqual(currentForm.includeImage.disabled, true);
                assert.strictEqual(currentForm.includeImage.checked, false);
                assert.strictEqual(currentForm.previewState.className, 'preview-state is-unavailable');
                assert.strictEqual(
                    currentForm.previewState.textContent,
                    '현재 카메라 이미지를 사용할 수 없습니다. 텍스트 기록은 계속할 수 있습니다.',
                );
            })().catch(error => { console.error(error); process.exitCode = 1; });
        """)

    def test_current_preview_fetch_rejection_uses_error_state(self):
        self.run_scenario(r"""
            (async () => {
                const opening = controller.open();
                await tick();
                requests[0].reject(new Error('network unavailable'));
                await opening;
                assert.strictEqual(currentForm.preview.hidden, true);
                assert.strictEqual(currentForm.includeImage.disabled, true);
                assert.strictEqual(currentForm.includeImage.checked, false);
                assert.strictEqual(currentForm.previewState.className, 'preview-state is-unavailable');
                assert.strictEqual(
                    currentForm.previewState.textContent,
                    '미리보기를 불러오지 못했습니다. 텍스트 기록은 계속할 수 있습니다.',
                );
            })().catch(error => { console.error(error); process.exitCode = 1; });
        """)

    def test_current_preview_invalid_content_type_uses_error_state(self):
        self.run_scenario(r"""
            (async () => {
                const opening = controller.open();
                await tick();
                requests[0].resolve({
                    ok: true,
                    status: 200,
                    headers: {
                        get(name) {
                            return name === 'Content-Type' ? 'application/json' : 'unexpected-token';
                        },
                    },
                });
                await opening;
                assert.strictEqual(currentForm.preview.hidden, true);
                assert.strictEqual(currentForm.includeImage.disabled, true);
                assert.strictEqual(currentForm.includeImage.checked, false);
                assert.strictEqual(currentForm.previewState.className, 'preview-state is-unavailable');
                assert.strictEqual(
                    currentForm.previewState.textContent,
                    '미리보기를 불러오지 못했습니다. 텍스트 기록은 계속할 수 있습니다.',
                );
            })().catch(error => { console.error(error); process.exitCode = 1; });
        """)

    def test_current_preview_missing_frame_token_uses_error_state(self):
        self.run_scenario(r"""
            (async () => {
                const opening = controller.open();
                await tick();
                requests[0].resolve({
                    ok: true,
                    status: 200,
                    headers: {
                        get(name) {
                            return name === 'Content-Type' ? 'image/jpeg' : null;
                        },
                    },
                });
                await opening;
                assert.strictEqual(currentForm.preview.hidden, true);
                assert.strictEqual(currentForm.includeImage.disabled, true);
                assert.strictEqual(currentForm.includeImage.checked, false);
                assert.strictEqual(currentForm.previewState.className, 'preview-state is-unavailable');
                assert.strictEqual(
                    currentForm.previewState.textContent,
                    '미리보기를 불러오지 못했습니다. 텍스트 기록은 계속할 수 있습니다.',
                );
            })().catch(error => { console.error(error); process.exitCode = 1; });
        """)

    def test_successful_preview_close_and_reopen_revokes_each_object_url(self):
        self.run_scenario(r"""
            (async () => {
                const firstOpen = controller.open();
                await tick();
                requests[0].resolve(previewResponse('first-url'));
                await firstOpen;
                assert.deepStrictEqual(revokedUrls, []);
                controller.close();
                assert.deepStrictEqual(revokedUrls, ['blob:first-url']);

                const secondOpen = controller.open();
                await tick();
                requests[1].resolve(previewResponse('second-url'));
                await secondOpen;
                controller.close();
                assert.deepStrictEqual(revokedUrls, ['blob:first-url', 'blob:second-url']);
            })().catch(error => { console.error(error); process.exitCode = 1; });
        """)

    def test_controller_reset_and_cancel_clear_without_dismiss_interception(self):
        self.run_scenario(r"""
            (async () => {
                async function openReady(token) {
                    const opening = controller.open();
                    await tick();
                    requests.at(-1).resolve(previewResponse(token));
                    await opening;
                    manager.stack.push({ name: 'old-view' });
                }
                function assertCleared() {
                    assert.strictEqual(manager.activeView, null);
                    assert.strictEqual(manager.stack.length, 0);
                    assert.strictEqual(modalRoot.style.display, 'none');
                }

                await openReady('reset');
                controller.reset();
                assertCleared();

                await openReady('cancel');
                await currentForm.cancel.emit('click');
                assertCleared();

                assert.strictEqual((closeButton.listeners.click || []).length, 0);
                assert.strictEqual((modalRoot.listeners.click || []).length, 0);
            })().catch(error => { console.error(error); process.exitCode = 1; });
        """)

    def test_save_success_and_stale_save_response_use_single_close_lifecycle(self):
        self.run_scenario(r"""
            (async () => {
                const opening = controller.open();
                await tick();
                requests[0].resolve(previewResponse('save-frame'));
                await opening;
                currentForm.description.value = '현재 상황';
                const submitting = currentForm.form.emit('submit', { preventDefault() {} });
                await tick();
                const saveRequest = requests[1];
                assert.strictEqual(saveRequest.url, '/api/logs/current-situation');
                assert.deepStrictEqual(JSON.parse(saveRequest.options.body), {
                    description_content: '현재 상황',
                    include_image: true,
                    frame_token: 'save-frame',
                });
                saveRequest.resolve(savedResponse());
                await submitting;
                assert.strictEqual(manager.activeView, null);
                assert.strictEqual(manager.stack.length, 0);
                assert.deepStrictEqual(alerts, ['현재 상황이 기록되었습니다.']);

                const staleOpening = controller.open();
                await tick();
                requests[2].resolve(previewResponse('stale-save-frame'));
                await staleOpening;
                currentForm.description.value = '이전 상황';
                const staleSubmit = currentForm.form.emit('submit', { preventDefault() {} });
                await tick();
                const staleSave = requests[3];
                controller.close();
                const freshOpening = controller.open();
                await tick();
                const freshPreview = requests[4];
                staleSave.resolve(savedResponse());
                await staleSubmit;
                assert.strictEqual(manager.activeView.name, 'currentSituation');
                assert.strictEqual(alerts.length, 1);
                freshPreview.resolve(previewResponse('fresh-after-save'));
                await freshOpening;
            })().catch(error => { console.error(error); process.exitCode = 1; });
        """)


if __name__ == "__main__":
    unittest.main()
