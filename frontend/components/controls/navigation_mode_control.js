(function initializeNavigationModeControl(global) {
    'use strict';

    const components = global.DabomDashboardComponents = global.DabomDashboardComponents || {};
    const controls = components.controls = components.controls || {};
    const instances = controls.navigationModes = controls.navigationModes || [];

    function canRender(root) {
        return Boolean(root?.ownerDocument?.createElement && root.replaceChildren);
    }

    function render(root, controller) {
        if (!canRender(root)) return;

        const document = root.ownerDocument;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'navigation-mode-toggle';
        button.setAttribute('aria-label', 'Mapping / Driving 전환');

        const mappingLabel = document.createElement('span');
        mappingLabel.className = 'navigation-mode-toggle-label';
        mappingLabel.dataset.navigationModeTarget = 'MAPPING';
        mappingLabel.textContent = 'MAPPING';

        const track = document.createElement('span');
        track.className = 'navigation-mode-toggle-track';
        track.setAttribute('aria-hidden', 'true');

        const slider = document.createElement('span');
        slider.className = 'navigation-mode-toggle-slider';
        track.append(slider);

        const drivingLabel = document.createElement('span');
        drivingLabel.className = 'navigation-mode-toggle-label';
        drivingLabel.dataset.navigationModeTarget = 'DRIVING';
        drivingLabel.textContent = 'DRIVING';

        button.append(mappingLabel, track, drivingLabel);
        root.replaceChildren(button);

        controller.button = button;
        controller.mappingLabel = mappingLabel;
        controller.drivingLabel = drivingLabel;

        button.addEventListener('click', event => {
            event.preventDefault();
            event.stopPropagation();
            if (controller.pending) return;

            const current = controller.mode;
            const explicitTarget = event.target?.closest?.(
                '[data-navigation-mode-target]'
            )?.dataset?.navigationModeTarget;
            const next = explicitTarget
                || (current === 'MAPPING' ? 'DRIVING' : 'MAPPING');

            if (next === current) return;
            controller.request(next);
        });
    }

    controls.mountNavigationMode = function mountNavigationMode(root, handlers = {}) {
        if (!root) return null;
        root.dataset.dashboardComponent = 'navigation-mode-control';

        const existing = root._dabomNavigationModeController;
        if (existing) {
            existing.handlers = { ...existing.handlers, ...handlers };
            return existing;
        }

        const controller = {
            root,
            handlers,
            mode: '',
            pending: false,
            button: null,
            mappingLabel: null,
            drivingLabel: null,

            request(mode) {
                const normalized = String(mode || '').toUpperCase();
                if (!['MAPPING', 'DRIVING'].includes(normalized)) return false;
                const request = this.handlers.request || global.navigationControl?.requestNavigationMode;
                return request?.(normalized);
            },

            sync(mode, options = {}) {
                this.mode = String(mode || '').toUpperCase();
                this.pending = Boolean(options.pending);
                root.dataset.navigationMode = this.mode;

                const isMapping = this.mode === 'MAPPING';
                const isDriving = this.mode === 'DRIVING';

                this.button?.classList.toggle('mapping', isMapping);
                this.button?.classList.toggle('driving', isDriving);
                this.button?.classList.toggle('unknown', !isMapping && !isDriving);
                if (this.button) {
                    this.button.disabled = this.pending;
                    this.button.setAttribute('aria-busy', String(this.pending));
                    this.button.setAttribute(
                        'aria-label',
                        isMapping
                            ? '현재 Mapping. Driving으로 전환'
                            : isDriving
                                ? '현재 Driving. Mapping으로 전환'
                                : 'Navigation mode 선택',
                    );
                }

                this.mappingLabel?.classList.toggle('active', isMapping);
                this.drivingLabel?.classList.toggle('active', isDriving);
                this.handlers.sync?.(this.mode, options);
            },
        };

        render(root, controller);
        root._dabomNavigationModeController = controller;
        instances.push(controller);
        controls.navigationMode = controller;
        controller.sync('', { pending: false });
        return controller;
    };

    controls.syncNavigationMode = function syncNavigationMode(mode, options = {}) {
        instances.forEach(controller => controller.sync(mode, options));
    };
})(window);
