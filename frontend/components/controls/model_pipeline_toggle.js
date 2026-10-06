(function initializeModelPipelineToggle(global) {
    'use strict';

    const components = global.DabomDashboardComponents = global.DabomDashboardComponents || {};
    const controls = components.controls = components.controls || {};

    function mount(root, handlers = {}) {
        if (!root) return null;

        const existing = root._dabomModelPipelineToggleController;
        if (existing) {
            existing.handlers = { ...existing.handlers, ...handlers };
            return existing;
        }

        const document = root.ownerDocument;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'dashboard-model-toggle';
        button.setAttribute('aria-label', 'Model pipeline ON / OFF');

        const label = document.createElement('span');
        label.className = 'dashboard-model-toggle-label';
        label.textContent = 'MODEL';

        const track = document.createElement('span');
        track.className = 'dashboard-model-toggle-track';
        track.setAttribute('aria-hidden', 'true');

        const slider = document.createElement('span');
        slider.className = 'dashboard-model-toggle-slider';
        track.append(slider);

        const stateLabel = document.createElement('span');
        stateLabel.className = 'dashboard-model-toggle-state';
        stateLabel.textContent = '--';

        button.append(label, track, stateLabel);
        root.replaceChildren(button);

        const controller = {
            root,
            button,
            handlers,
            enabled: null,
            connected: null,
            pending: false,
            error: null,

            request() {
                if (this.connected !== true || this.pending) return false;
                const next = this.enabled !== true;
                return this.handlers.request?.(next);
            },

            sync(options = {}) {
                if (Object.prototype.hasOwnProperty.call(options, 'enabled')) {
                    this.enabled = typeof options.enabled === 'boolean'
                        ? options.enabled
                        : null;
                }
                if (Object.prototype.hasOwnProperty.call(options, 'connected')) {
                    this.connected = options.connected;
                }
                if (Object.prototype.hasOwnProperty.call(options, 'pending')) {
                    this.pending = Boolean(options.pending);
                }
                if (Object.prototype.hasOwnProperty.call(options, 'error')) {
                    this.error = options.error || null;
                }

                const active = this.enabled === true;
                const known = this.enabled !== null;
                button.classList.toggle('active', active);
                button.classList.toggle('unknown', !known);
                button.setAttribute('aria-pressed', String(active));
                button.setAttribute('aria-busy', String(this.pending));
                button.disabled = this.connected !== true || this.pending;
                stateLabel.textContent = known ? (active ? 'ON' : 'OFF') : '--';

                if (this.pending) {
                    button.title = active
                        ? 'Model pipeline을 끄는 중입니다.'
                        : 'Model pipeline을 켜는 중입니다.';
                } else if (this.error) {
                    button.title = this.error;
                } else if (this.connected !== true) {
                    button.title = 'GPU 서버 상태를 확인할 수 없습니다.';
                } else {
                    button.title = active
                        ? 'Model pipeline 끄기'
                        : 'Model pipeline 켜기';
                }
            },
        };

        button.addEventListener('click', event => {
            event.preventDefault();
            event.stopPropagation();
            controller.request();
        });

        root._dabomModelPipelineToggleController = controller;
        controls.modelPipelineToggle = controller;
        controller.sync();
        return controller;
    }

    controls.mountModelPipelineToggle = mount;
})(window);
