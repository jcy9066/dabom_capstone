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
        // Preserve the initially rendered UI; attach behavior without DOM swap.
        let button = root.querySelector('.dashboard-led-toggle');
        let label = button?.querySelector('.dashboard-led-toggle-label');
        let track = button?.querySelector('.dashboard-led-toggle-track');
        let slider = track?.querySelector('.dashboard-led-toggle-slider');
        let stateLabel = button?.querySelector('.dashboard-led-toggle-state');
        let offLabel = button?.querySelector('.ui-toggle-side-off');
        let onLabel = button?.querySelector('.ui-toggle-side-on');
        if (!button || !label || !track || !slider || !stateLabel || !offLabel || !onLabel) {
            button = document.createElement('button');
            button.type = 'button';
            button.className = 'dashboard-led-toggle';
            button.setAttribute('aria-label', 'LED ON / OFF');

            label = document.createElement('span');
            label.className = 'dashboard-led-toggle-label';
            label.textContent = 'LED';

            track = document.createElement('span');
            track.className = 'dashboard-led-toggle-track';
            track.setAttribute('aria-hidden', 'true');

            slider = document.createElement('span');
            slider.className = 'dashboard-led-toggle-slider';
            track.append(slider);

            stateLabel = document.createElement('span');
            stateLabel.className = 'dashboard-led-toggle-state';
            stateLabel.textContent = '--';

            offLabel = document.createElement('span');
            offLabel.className = 'ui-toggle-side ui-toggle-side-off';
            offLabel.textContent = 'OFF';
            onLabel = document.createElement('span');
            onLabel.className = 'ui-toggle-side ui-toggle-side-on';
            onLabel.textContent = 'ON';
            button.append(label, offLabel, track, onLabel, stateLabel);
            root.replaceChildren(button);


        }

        const controller = {
            root,
            button,
            handlers,
            enabled: false,
            connected: null,
            pending: false,

            request() {
                if (this.connected !== true || this.pending) return false;
                const next = this.enabled !== true;
                return this.handlers.request?.(next);
            },

            sync(options = {}) {
                if (Object.prototype.hasOwnProperty.call(options, 'enabled')) {
                    // The Pi firmware starts with LED OFF. Use OFF for missing
                    // telemetry until a real boolean state arrives; never send
                    // an automatic control command from this display fallback.
                    this.enabled = typeof options.enabled === 'boolean'
                        ? options.enabled
                        : false;
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
                offLabel.classList.toggle('active', known && !active);
                onLabel.classList.toggle('active', known && active);
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
