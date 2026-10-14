(function initializeCurrentSituation(global) {
    'use strict';

    const components = global.DabomDashboardComponents = global.DabomDashboardComponents || {};
    const currentSituation = components.currentSituation = components.currentSituation || {};
    const VIEW_NAME = 'currentSituation';

    function findWithin(root, selector) {
        return root?.querySelector?.(selector) || global.document?.querySelector?.(selector) || null;
    }

    function autoGrowTextarea(textarea) {
        if (!textarea) return;
        textarea.style.resize = 'none';
        textarea.style.overflowY = 'hidden';
        textarea.style.height = 'auto';
        textarea.style.height = `${textarea.scrollHeight}px`;
    }

    function renderForm() {
        return `
            <form class="current-situation-form">
                <div class="current-situation-layout">
                    <div class="current-situation-media-box">
                        <div class="privacy-preview-frame" aria-label="현재 카메라 이미지">
                    <img id="currentSituationPreview" alt="개인정보 보호 처리된 현재 카메라 미리보기" hidden>
                    <div id="currentSituationPreviewState" class="preview-state is-loading">개인정보 보호 미리보기를 불러오는 중입니다.</div>
                        </div>
                    </div>
                    <div class="current-situation-entry-box">
                        <label class="record-field-label" for="currentSituationDescription">상황 내용</label>
                <textarea id="currentSituationDescription" rows="5" placeholder="현재 상황을 입력하세요. 예: 1층 출입구 주변 확인 필요"></textarea>
                <label class="record-image-option">
                    <input id="currentSituationIncludeImage" type="checkbox" disabled>
                    <span>위 이미지를 함께 저장</span>
                </label>
                <p class="record-requirement">※ 텍스트 또는 이미지 중 최소 하나는 기록해야 합니다.</p>
                <div id="currentSituationError" class="record-error" role="alert"></div>
                <div class="record-form-actions">
                    <button id="currentSituationSubmit" type="submit" class="filter-btn">기록</button>
                        </div>
                    </div>
                </div>
            </form>`;
    }

    currentSituation.mount = function mountCurrentSituation(root, handlers = {}) {
        if (!root) return null;
        root.dataset.dashboardComponent = 'current-situation';

        const manager = components.modal?.getDefault?.() || null;
        const modalRoot = manager?.root || global.document?.querySelector?.('#commonModal') || null;
        const modalBody = manager?.body || global.document?.querySelector?.('#modalBody') || null;
        const modalTitle = manager?.title || global.document?.querySelector?.('#modalTitle') || null;
        const state = {
            generation: 0,
            abortController: null,
            frameToken: null,
            previewUrl: null,
            submitting: false,
        };
        let controller = null;

        function isActive(generation) {
            const managerActive = !manager || manager.activeView?.name === VIEW_NAME;
            return generation === state.generation && managerActive;
        }

        function invalidate() {
            state.generation += 1;
            state.abortController?.abort();
            state.abortController = null;
            if (state.previewUrl) global.URL?.revokeObjectURL?.(state.previewUrl);
            state.previewUrl = null;
            state.frameToken = null;
            state.submitting = false;
        }

        function beginGeneration() {
            invalidate();
            state.abortController = typeof global.AbortController === 'function'
                ? new global.AbortController()
                : null;
            return state.generation;
        }

        async function csrfToken(generation) {
            if (typeof global.dashboardRecordsCsrfToken === 'function') {
                const token = await global.dashboardRecordsCsrfToken();
                return isActive(generation) ? token : null;
            }
            const response = await global.fetch('/api/auth/csrf', {
                credentials: 'same-origin',
                signal: state.abortController?.signal,
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok || !data.csrf_token) throw new Error('보안 토큰을 준비하지 못했습니다.');
            return isActive(generation) ? data.csrf_token : null;
        }

        function formElements() {
            return {
                form: findWithin(modalBody, '.current-situation-form'),
                preview: findWithin(modalBody, '#currentSituationPreview'),
                previewState: findWithin(modalBody, '#currentSituationPreviewState'),
                description: findWithin(modalBody, '#currentSituationDescription'),
                includeImage: findWithin(modalBody, '#currentSituationIncludeImage'),
                error: findWithin(modalBody, '#currentSituationError'),
                submit: findWithin(modalBody, '#currentSituationSubmit'),
                cancel: findWithin(modalBody, '[data-current-situation-close]'),
            };
        }

        function resetPreviewOption(elements) {
            if (!elements.includeImage) return;
            elements.includeImage.disabled = true;
            elements.includeImage.checked = false;
        }

        async function loadPreview(generation, elements) {
            resetPreviewOption(elements);
            try {
                const token = await csrfToken(generation);
                if (!token || !isActive(generation)) return;
                const response = await global.fetch('/api/logs/current-situation/preview', {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'X-CSRF-Token': token },
                    signal: state.abortController?.signal,
                });
                if (response.status === 503) throw new Error('PREVIEW_UNAVAILABLE');
                if (!response.ok || !response.headers.get('Content-Type')?.includes('image/jpeg')) {
                    throw new Error('PREVIEW_FAILED');
                }
                const frameToken = response.headers.get('X-Frame-Token');
                if (!frameToken) throw new Error('PREVIEW_FAILED');
                const blob = await response.blob();
                if (!isActive(generation)) return;

                state.frameToken = frameToken;
                state.previewUrl = global.URL.createObjectURL(blob);
                elements.preview.src = state.previewUrl;
                elements.preview.hidden = false;
                elements.previewState.hidden = true;
                elements.includeImage.disabled = false;
                elements.includeImage.checked = true;
            } catch (error) {
                if (!isActive(generation) || error?.name === 'AbortError') return;
                elements.previewState.className = 'preview-state is-unavailable';
                elements.previewState.textContent = error.message === 'PREVIEW_UNAVAILABLE'
                    ? '현재 카메라 이미지를 사용할 수 없습니다. 텍스트 기록은 계속할 수 있습니다.'
                    : '미리보기를 불러오지 못했습니다. 텍스트 기록은 계속할 수 있습니다.';
                resetPreviewOption(elements);
            }
        }

        async function submit(event, generation, elements) {
            event.preventDefault();
            if (state.submitting || !isActive(generation)) return;
            const description = elements.description.value.trim();
            const includeImage = elements.includeImage.checked;
            if (!description && !includeImage) {
                elements.error.textContent = '텍스트 또는 이미지 중 최소 하나를 기록해 주세요.';
                return;
            }
            if (includeImage && !state.frameToken) {
                elements.error.textContent = '저장할 이미지 토큰이 유효하지 않습니다. 미리보기를 다시 열어 주세요.';
                return;
            }

            state.submitting = true;
            elements.error.textContent = '';
            elements.submit.disabled = true;
            elements.submit.textContent = '기록 중...';
            try {
                const token = await csrfToken(generation);
                if (!token || !isActive(generation)) return;
                const response = await global.fetch('/api/logs/current-situation', {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRF-Token': token,
                    },
                    body: JSON.stringify({
                        description_content: description,
                        include_image: includeImage,
                        frame_token: includeImage ? state.frameToken : null,
                    }),
                    signal: state.abortController?.signal,
                });
                const data = await response.json().catch(() => ({}));
                if (!isActive(generation)) return;
                if (response.status !== 201) {
                    throw new Error(data.detail || data.error || `기록 실패 (HTTP ${response.status})`);
                }
                controller.close();
                global.alert?.('현재 상황이 기록되었습니다.');
            } catch (error) {
                if (!isActive(generation) || error?.name === 'AbortError') return;
                elements.error.textContent = error.message || '현재 상황을 기록하지 못했습니다.';
                elements.submit.disabled = false;
                elements.submit.textContent = '기록';
                state.submitting = false;
            }
        }

        function activateForm() {
            const generation = beginGeneration();
            const elements = formElements();
            if (!elements.form || !elements.description || !elements.includeImage) return;
            elements.description.addEventListener('input', () => autoGrowTextarea(elements.description));
            autoGrowTextarea(elements.description);
            elements.cancel?.addEventListener('click', () => controller.close());
            elements.form.addEventListener('submit', event => submit(event, generation, elements));
            return loadPreview(generation, elements);
        }

        function closeCurrentView() {
            if (manager) {
                manager.close();
                return;
            }
            invalidate();
            if (modalRoot) modalRoot.style.display = 'none';
            handlers.close?.();
        }

        controller = {
            root,
            open() {
                if (manager) {
                    manager.close();
                    return manager.open(VIEW_NAME, {}, { replace: true });
                }
                invalidate();
                if (modalTitle) modalTitle.textContent = '상황 기록';
                if (modalBody) modalBody.innerHTML = renderForm();
                if (modalRoot) modalRoot.style.display = 'flex';
                return activateForm();
            },
            close: closeCurrentView,
            reset: closeCurrentView,
        };

        if (manager) {
            manager.register(VIEW_NAME, {
                title: '상황 기록',
                render: renderForm,
                onOpen: activateForm,
                onClose: invalidate,
            });
        }

        const trigger = root.querySelector?.('[data-record-view="currentSituation"]');
        if (trigger) {
            trigger.removeAttribute('onclick');
            trigger.addEventListener('click', event => {
                event.preventDefault();
                controller.open();
            });
        }

        currentSituation.controller = controller;
        currentSituation.autoGrowTextarea = autoGrowTextarea;
        return controller;
    };
})(window);
