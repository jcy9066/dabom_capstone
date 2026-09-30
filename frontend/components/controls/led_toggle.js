(function initializeLedToggle(global) {
    'use strict';

    const components = global.DabomDashboardComponents = global.DabomDashboardComponents || {};
    const controls = components.controls = components.controls || {};

    function mount(root, handlers = {}) {
        if (!root) return null;

        const existing = root._dabomLedToggleController;
        if (existing) {
            existing.handlers = { ...existing.handlers, ...handlers };
            return existing;
        }

        const document = root.ownerDocument;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'dashboard-led-toggle';
        button.setAttribute('aria-label', 'LED ON / OFF');

        const label = document.createElement('span');
        label.className = 'dashboard-led-toggle-label';
        label.textContent = 'LED';

        const track = document.createElement('span');
        track.className = 'dashboard-led-toggle-track';
        track.setAttribute('aria-hidden', 'true');

        const slider = document.createElement('span');
        slider.className = 'dashboard-led-toggle-slider';
        track.append(slider);

        const stateLabel = document.createElement('span');
        stateLabel.className = 'dashboard-led-toggle-state';
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

                const active = this.enabled === true;
                const known = this.enabled !== null;
                button.classList.toggle('active', active);
                button.classList.toggle('unknown', !known);
                button.setAttribute('aria-pressed', String(active));
                button.setAttribute('aria-busy', String(this.pending));
                button.disabled = this.connected !== true || this.pending;
                stateLabel.textContent = known ? (active ? 'ON' : 'OFF') : '--';
                button.title = this.connected !== true
                    ? 'Pi 연결 후 LED를 제어할 수 있습니다.'
                    : active
                        ? 'LED 끄기'
                        : 'LED 켜기';
            },
        };

        button.addEventListener('click', event => {
            event.preventDefault();
            event.stopPropagation();
            controller.request();
        });

        root._dabomLedToggleController = controller;
        controls.ledToggle = controller;
        controller.sync();
        return controller;
    }

    controls.mountLedToggle = mount;
})(window);
