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

        const button = root.ownerDocument.createElement('button');
        button.type = 'button';
        button.textContent = 'LED';
        button.dataset.controlButton = 'secondary';
        button.className = 'lidar-led-toggle';
        controls.controlButton?.enhance?.(button);
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
                button.classList.toggle('active', active);
                button.classList.toggle('unknown', this.enabled === null);
                button.setAttribute('aria-pressed', String(active));
                button.setAttribute('aria-busy', String(this.pending));
                button.disabled = this.connected !== true || this.pending;
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
